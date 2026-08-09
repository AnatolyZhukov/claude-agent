"""Chat history and rating logging to BigQuery.

Both tables are append-only and written through batch load jobs — see
`_append_row` for why. Every public function degrades to a no-op when
GOOGLE_APPLICATION_CREDENTIALS_JSON isn't set, so the app runs fine without
BigQuery configured.
"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.oauth2 import service_account

DATASET_ID = "claude_agent"
TABLE_ID = "chat_history"
RATINGS_TABLE_ID = "ratings"
LOCATION = "US"
PARTITION_FIELD = "event_date"

_SCHEMA = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    # Date-only copy of `timestamp`, used purely as the partitioning column.
    # BigQuery time-unit partitioning needs a DATE (or a TIMESTAMP truncated
    # to DAY), and "date" itself is a reserved word in Standard SQL, so this
    # is named event_date instead of `date` to avoid needing backticks on
    # every query that touches it.
    bigquery.SchemaField("event_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("answer", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
]

# Ratings live in their own append-only table rather than a `rating` column
# updated in place on chat_history: the GCP project has no billing account,
# which puts BigQuery in "sandbox mode" — confirmed by testing that this
# rejects UPDATE the same way it rejects INSERT/streaming ("DML queries are
# not allowed in the free tier"). Every rating click is a new row here
# instead; get_recent_history() joins in only the latest one per
# interaction_id.
_RATINGS_SCHEMA = [
    bigquery.SchemaField("interaction_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("rating", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("event_date", "DATE", mode="REQUIRED"),
]


@dataclass(frozen=True)
class _Destination:
    """A resolved write target: the client to use and the table to write to.

    Returned as a pair rather than stashing the client in a module global and
    reading it back later, so no function depends on another having run first.
    """

    client: bigquery.Client
    table_ref: bigquery.TableReference
    schema: list


_client: bigquery.Client | None = None
_known_tables: set = set()


def _build_client() -> bigquery.Client | None:
    """Builds a BigQuery client from the service account JSON in the
    environment, or returns None if it isn't configured.

    GOOGLE_APPLICATION_CREDENTIALS_JSON holds the *content* of a service
    account key (not a path) so the same value works locally via .env and on
    Streamlit Community Cloud via st.secrets, where there is no local
    filesystem to point a path at.
    """
    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(credentials=credentials, project=info["project_id"])


def _resolve(table_id: str, schema: list) -> _Destination | None:
    """Returns the write destination for `table_id`, creating the dataset and
    table on first use, or None if BigQuery isn't configured.

    Lazy creation instead of a separate setup script, so a fresh GCP project
    needs no manual provisioning step.
    """
    global _client
    if _client is None:
        _client = _build_client()
    if _client is None:
        return None
    client = _client

    dataset_ref = bigquery.DatasetReference(client.project, DATASET_ID)
    table_ref = dataset_ref.table(table_id)
    if table_id in _known_tables:
        return _Destination(client, table_ref, schema)

    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = LOCATION
        client.create_dataset(dataset)

    try:
        client.get_table(table_ref)
    except NotFound:
        table = bigquery.Table(table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=PARTITION_FIELD
        )
        client.create_table(table)

    _known_tables.add(table_id)
    return _Destination(client, table_ref, schema)


def _append_row(destination: _Destination, row: dict) -> None:
    """Appends one row via a batch load job.

    Not a streaming insert (Client.insert_rows_json) or a DML INSERT query
    job: a GCP project with no billing account linked runs BigQuery in
    "sandbox mode", which rejects both of those outright ("Streaming insert is
    not allowed in the free tier" / "DML queries are not allowed in the free
    tier") but still allows load jobs. The same free-tier limits apply as any
    other sandbox project (e.g. 10GB storage, 1TB queries/month) — plenty for
    this project's traffic.
    """
    job_config = bigquery.LoadJobConfig(
        schema=destination.schema,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    destination.client.load_table_from_json(
        [row], destination.table_ref, job_config=job_config
    ).result()


def _now_fields() -> dict:
    """The timestamp/event_date pair both tables carry, from a single clock
    read so the partition always matches the timestamp.
    """
    now = datetime.now(UTC)
    return {"timestamp": now.isoformat(), "event_date": now.date().isoformat()}


def log_interaction(question: str, answer: str, content_turns: list) -> str | None:
    """Writes one row per ask() call: the question, the final answer text, and
    the raw content blocks (text/tool_use) of every model turn along the way,
    serialized as JSON — kept separate from `answer` so the full detail of what
    the model actually did is available without changing what's shown to the
    user. Returns the row's id (for log_rating), or None if BigQuery isn't
    configured.
    """
    destination = _resolve(TABLE_ID, _SCHEMA)
    if destination is None:
        return None

    interaction_id = str(uuid.uuid4())
    _append_row(destination, {
        "id": interaction_id,
        **_now_fields(),
        "question": question,
        "answer": answer,
        "content": json.dumps(content_turns, ensure_ascii=False),
    })
    return interaction_id


def log_rating(interaction_id: str, rating: str | None) -> None:
    """Appends a rating event for a prior interaction ("up"/"down"/None for a
    cleared selection). See the comment on _RATINGS_SCHEMA for why this is
    append-only rather than an UPDATE.
    """
    destination = _resolve(RATINGS_TABLE_ID, _RATINGS_SCHEMA)
    if destination is None:
        return

    _append_row(destination, {
        "interaction_id": interaction_id,
        "rating": rating,
        **_now_fields(),
    })


def get_recent_history(limit: int = 20, days: int = 5) -> list[dict]:
    """Most recent interactions, newest first, restricted to the last `days`
    days — a plain WHERE on the partitioning column (event_date) so BigQuery
    prunes to just those partitions instead of scanning the whole table. Each
    row's `rating` is the most recent rating event for that interaction, if any
    (NULL otherwise).
    """
    destination = _resolve(TABLE_ID, _SCHEMA)
    if destination is None:
        return []
    # Resolved even though its reference isn't used directly: the JOIN below
    # names this table, which errors if it doesn't exist yet (e.g. no rating
    # has ever been given).
    _resolve(RATINGS_TABLE_ID, _RATINGS_SCHEMA)

    client = destination.client
    # limit/days are internal ints (not user-controlled text), so plain
    # interpolation is fine here — BigQuery doesn't support parameterizing
    # LIMIT/INTERVAL literals the way it does WHERE-clause values.
    query = f"""
        WITH latest_rating AS (
            SELECT interaction_id, rating,
                   ROW_NUMBER() OVER (
                       PARTITION BY interaction_id ORDER BY timestamp DESC
                   ) AS rn
            FROM `{client.project}.{DATASET_ID}.{RATINGS_TABLE_ID}`
        )
        SELECT h.timestamp, h.question, h.answer, r.rating
        FROM `{client.project}.{DATASET_ID}.{TABLE_ID}` h
        LEFT JOIN latest_rating r ON r.interaction_id = h.id AND r.rn = 1
        WHERE h.event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)
        ORDER BY h.timestamp DESC
        LIMIT {int(limit)}
    """
    return [dict(row) for row in client.query(query).result()]
