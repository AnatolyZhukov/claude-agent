import json
import os
import uuid
from datetime import datetime, timezone

from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.oauth2 import service_account

DATASET_ID = "claude_agent"
TABLE_ID = "chat_history"
RATINGS_TABLE_ID = "ratings"
LOCATION = "US"

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

_client = None
_table_refs = {}


def _build_client():
    # GOOGLE_APPLICATION_CREDENTIALS_JSON holds the *content* of a service
    # account key (not a path) so the same value works locally via .env and
    # on Streamlit Community Cloud via st.secrets, where there is no local
    # filesystem to point a path at.
    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(credentials=credentials, project=info["project_id"])


def _ensure_table(table_id: str, schema: list, partition_field: str):
    # Lazily creates the dataset/table on first use instead of a separate
    # setup script, so a fresh GCP project needs no manual provisioning step.
    global _client
    if table_id in _table_refs:
        return _table_refs[table_id]

    client = _client or _build_client()
    if client is None:
        return None
    _client = client

    dataset_ref = bigquery.DatasetReference(client.project, DATASET_ID)
    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = LOCATION
        client.create_dataset(dataset)

    table_ref = dataset_ref.table(table_id)
    try:
        client.get_table(table_ref)
    except NotFound:
        table = bigquery.Table(table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=partition_field
        )
        client.create_table(table)

    _table_refs[table_id] = table_ref
    return table_ref


def log_interaction(question: str, answer: str, content_turns: list) -> str | None:
    """Writes one row per ask() call: the question, the final answer text,
    and the raw content blocks (text/tool_use) of every model turn along the
    way, serialized as JSON — kept separate from `answer` so the full detail
    of what the model actually did is available without changing what's
    shown to the user. Returns the row's id (for log_rating), or None if
    BigQuery isn't configured.
    """
    table_ref = _ensure_table(TABLE_ID, _SCHEMA, "event_date")
    if table_ref is None:
        return None

    interaction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    row = {
        "id": interaction_id,
        "timestamp": now.isoformat(),
        "event_date": now.date().isoformat(),
        "question": question,
        "answer": answer,
        "content": json.dumps(content_turns, ensure_ascii=False),
    }
    # A batch load job, not a streaming insert (Client.insert_rows_json) or
    # a DML INSERT query job: a GCP project with no billing account linked
    # runs BigQuery in "sandbox mode", which rejects both of those outright
    # ("Streaming insert is not allowed in the free tier" /
    # "DML queries are not allowed in the free tier") but still allows load
    # jobs. Same free-tier limits apply as any other sandbox project (e.g.
    # 10GB storage, 1TB queries/month) — plenty for this project's traffic.
    job_config = bigquery.LoadJobConfig(
        schema=_SCHEMA, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
    )
    _client.load_table_from_json([row], table_ref, job_config=job_config).result()
    return interaction_id


def log_rating(interaction_id: str, rating: str | None) -> None:
    """Appends a rating event for a prior interaction ("up"/"down"/None for
    a cleared selection). See the module-level comment on _RATINGS_SCHEMA
    for why this is append-only rather than an UPDATE.
    """
    table_ref = _ensure_table(RATINGS_TABLE_ID, _RATINGS_SCHEMA, "event_date")
    if table_ref is None:
        return

    now = datetime.now(timezone.utc)
    row = {
        "interaction_id": interaction_id,
        "rating": rating,
        "timestamp": now.isoformat(),
        "event_date": now.date().isoformat(),
    }
    job_config = bigquery.LoadJobConfig(
        schema=_RATINGS_SCHEMA, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
    )
    _client.load_table_from_json([row], table_ref, job_config=job_config).result()


def get_recent_history(limit: int = 20, days: int = 5) -> list[dict]:
    """Most recent interactions, newest first, restricted to the last `days`
    days — a plain WHERE on the partitioning column (event_date) so BigQuery
    prunes to just those partitions instead of scanning the whole table.
    Each row's `rating` is the most recent rating event for that
    interaction, if any (NULL otherwise).
    """
    table_ref = _ensure_table(TABLE_ID, _SCHEMA, "event_date")
    if table_ref is None:
        return []
    # Ensured even though unused directly here: the JOIN below references
    # this table by name, which errors if it doesn't exist yet (e.g. no
    # rating has ever been given).
    _ensure_table(RATINGS_TABLE_ID, _RATINGS_SCHEMA, "event_date")

    # limit/days are internal ints (not user-controlled text), so plain
    # interpolation is fine here — BigQuery doesn't support parameterizing
    # LIMIT/INTERVAL literals the way it does WHERE-clause values.
    query = f"""
        WITH latest_rating AS (
            SELECT interaction_id, rating,
                   ROW_NUMBER() OVER (
                       PARTITION BY interaction_id ORDER BY timestamp DESC
                   ) AS rn
            FROM `{_client.project}.{DATASET_ID}.{RATINGS_TABLE_ID}`
        )
        SELECT h.timestamp, h.question, h.answer, r.rating
        FROM `{_client.project}.{DATASET_ID}.{TABLE_ID}` h
        LEFT JOIN latest_rating r ON r.interaction_id = h.id AND r.rn = 1
        WHERE h.event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {int(days)} DAY)
        ORDER BY h.timestamp DESC
        LIMIT {int(limit)}
    """
    rows = _client.query(query).result()
    return [dict(row) for row in rows]
