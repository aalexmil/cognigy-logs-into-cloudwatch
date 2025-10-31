import os, json, ssl, gzip, urllib.request, urllib.parse, datetime as dt
import boto3
from botocore.config import Config

REGION     = "eu-central-1"
PROJECT_ID = "123abcd4def567abcd890e12"
API_URL    = f"https://api-trial.cognigy.ai/new/v2.0/projects/{PROJECT_ID}/logs"
LOG_GROUP  = "cognigy-logs"
LOG_STREAM = "lambda-poller-stream"

# Parameter names
SSM_API_KEY   = "/cognigy-mytrial-api-key"    # SecureString
SSM_LAST_TS   = "/cognigy-last-ts-ms"         # String
SSM_SEQ_TOKEN = "/cognigy-last-seq-token"     # String (optional optimization)

# Prefer env var (provision via KMS-encrypted env var) to avoid SSM call
ENV_API_KEY = os.getenv("COGNIGY_API_KEY")

# Faster boto3 client with lower timeouts + keepalive
cfg  = Config(retries={"max_attempts": 2}, tcp_keepalive=True,
              connect_timeout=1, read_timeout=3)
ssm  = boto3.client("ssm",  region_name=REGION, config=cfg)
logs = boto3.client("logs", region_name=REGION, config=cfg)

# Reuse SSL context + HTTPS opener (HTTP/1.1 keep-alive)
_ssl_ctx = ssl.create_default_context()
_https_handler = urllib.request.HTTPSHandler(context=_ssl_ctx)
_opener = urllib.request.build_opener(_https_handler)
_opener.addheaders = [("Accept-Encoding", "gzip")]

def get_param_secret(name, default=None):
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return default

def get_param_plain(name, default=None):
    try:
        return ssm.get_parameter(Name=name, WithDecryption=False)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return default

def put_param(name, value):
    ssm.put_parameter(Name=name, Value=str(value), Type="String", Overwrite=True)

def iso_ms(z: str) -> int:
    # '2025-10-13T08:12:34.567Z' -> epoch ms
    if z.endswith("Z"):
        z = z[:-1] + "+00:00"
    return int(dt.datetime.fromisoformat(z).timestamp() * 1000)

def fetch(api_key, cursor=None):
    q = {"limit": "100", "sort": "timestamp:desc"}
    if cursor:
        q["next"] = cursor
    url = API_URL + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"X-API-KEY": api_key})
    with _opener.open(req) as r:
        data = r.read()
        if "gzip" in (r.headers.get("Content-Encoding", "")).lower():
            data = gzip.decompress(data)
        body = json.loads(data.decode())

    items = body.get("_embedded", {}).get("logEntry", [])
    href  = body.get("_links", {}).get("next", {}).get("href")
    nxt = None
    if href:
        nxt = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("next", [None])[0]
    return items, nxt

def put_events_with_retry(log_events, seq_token=None):
    args = {"logGroupName": LOG_GROUP, "logStreamName": LOG_STREAM, "logEvents": log_events}
    if seq_token:
        args["sequenceToken"] = seq_token
    try:
        resp = logs.put_log_events(**args)
        return resp.get("nextSequenceToken")
    except logs.exceptions.InvalidSequenceTokenException as e:
        # Retry once with the expected token from the error
        expected = e.response["Error"].get("expectedSequenceToken")
        if not expected:
            raise
        args["sequenceToken"] = expected
        resp = logs.put_log_events(**args)
        return resp.get("nextSequenceToken")
    except logs.exceptions.ResourceNotFoundException:
        # Stream might not exist yet: create and retry once
        logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=LOG_STREAM)
        resp = logs.put_log_events(**args)
        return resp.get("nextSequenceToken")

def lambda_handler(event, context):
    # -------- Params (minimize SSM + KMS) --------
    api_key = ENV_API_KEY or get_param_secret(SSM_API_KEY)
    if not api_key:
        raise RuntimeError("Missing API key: set COGNIGY_API_KEY env var or SSM parameter.")

    last_ts  = int(get_param_plain(SSM_LAST_TS, "0"))
    seq_tok  = get_param_plain(SSM_SEQ_TOKEN)  # optional optimization; may be None

    # -------- Fetch new logs (newest → oldest until last_ts) --------
    batch, newest_ts = [], last_ts
    cursor = None

    while True:
        page, nxt = fetch(api_key, cursor)
        if not page:
            break

        # If the newest on this page is already <= last_ts, we can stop immediately
        if iso_ms(page[0]["timestamp"]) <= last_ts:
            break

        for e in page:
            ts = iso_ms(e["timestamp"])
            if ts <= last_ts:
                page = []  # stop outer loop
                break
            # compact JSON to reduce bytes over CW Logs
            batch.append({"timestamp": ts, "message": json.dumps(e, separators=(",", ":"))})
            if ts > newest_ts:
                newest_ts = ts

        if not nxt or not page:
            break
        cursor = nxt

    if not batch:
        return {"message": "No new logs."}

    # CloudWatch Logs requires chronological order
    batch.sort(key=lambda ev: ev["timestamp"])

    # -------- Put to CloudWatch Logs (no describe) --------
    next_token = put_events_with_retry(batch, seq_tok)

    # -------- Persist progress (and seq token for next run) --------
    put_param(SSM_LAST_TS, newest_ts)
    if next_token:
        put_param(SSM_SEQ_TOKEN, next_token)

    return {"message": f"Pushed {len(batch)} logs.", "last_ts": newest_ts}
