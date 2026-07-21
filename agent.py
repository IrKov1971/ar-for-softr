"""
AR for Softr — QBO to Softr Database sync
"""
import os, base64, httpx
from datetime import date, datetime, timezone

QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API_BASE = "https://quickbooks.api.intuit.com/v3/company"
DAYS_AHEAD = 30

GITHUB_API = "https://api.github.com"
QBO_TOKEN_SOURCE_REPO = "IrKov1971/ar-for-comments"

SOFTR_API_BASE = "https://tables-api.softr.io/api/v1"


def get_qbo_refresh_token_from_github(gh_pat):
    headers = {
        "Authorization": f"token {gh_pat.strip()}",
        "Accept": "application/vnd.github+json",
    }
    r = httpx.get(
        f"{GITHUB_API}/repos/{QBO_TOKEN_SOURCE_REPO}/actions/variables/QBO_REFRESH_TOKEN",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["value"].strip()


def get_qbo_access_token(client_id, client_secret, refresh_token):
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    refresh_token = refresh_token.strip()

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = httpx.post(
        QBO_TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def qbo_query(realm_id, access_token, query):
    r = httpx.get(
        f"{QBO_API_BASE}/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": query},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def fetch_unpaid_invoices(realm_id, access_token):
    invoices, start, page = [], 1, 1000
    while True:
        data = qbo_query(realm_id, access_token, f"SELECT * FROM Invoice WHERE Balance > '0' STARTPOSITION {start} MAXRESULTS {page}")
        batch = data.get("QueryResponse", {}).get("Invoice", []) or []
        if not batch:
            break
        invoices.extend(batch)
        if len(batch) < page:
            break
        start += page
    return invoices


def fetch_customer_map(realm_id, access_token):
    customers = {}
    start, page = 1, 1000
    while True:
        data = qbo_query(realm_id, access_token, f"SELECT * FROM Customer STARTPOSITION {start} MAXRESULTS {page}")
        batch = data.get("QueryResponse", {}).get("Customer", []) or []
        if not batch:
            break
        for c in batch:
            customers[c["Id"]] = c
        if len(batch) < page:
            break
        start += page
    return customers


def enrich_invoices(invoices, customer_map):
    for inv in invoices:
        if not inv.get("ProjectRef"):
            continue
        ref = inv.get("CustomerRef", {})
        customer = customer_map.get(ref.get("value", ""))
        if customer and customer.get("ParentRef"):
            parent = customer_map.get(customer["ParentRef"]["value"])
            if parent:
                ref["name"] = f"{parent['DisplayName']}:{ref.get('name', '')}"


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def compute_status(due_date, today):
    delta = (due_date - today).days
    if delta < 0:
        return f"overdue {abs(delta)} days"
    elif delta == 0:
        return "due in 0 days"
    else:
        return f"due in {delta} days"


def filter_and_sort(invoices):
    today = date.today()
    result = [
        inv for inv in invoices
        if float(inv.get("Balance", 0) or 0) > 0
        and inv.get("DueDate")
        and (parse_date(inv["DueDate"]) - today).days <= DAYS_AHEAD
    ]
    result.sort(key=lambda inv: (parse_date(inv["DueDate"]) - today).days)
    return result


def format_fields(inv):
    today = date.today()
    balance = float(inv.get("Balance", 0) or 0)
    due = parse_date(inv.get("DueDate", ""))
    days_delta = (due - today).days
    return {
        "customer": inv.get("CustomerRef", {}).get("name", ""),
        "amount": balance,
        "due_date": due.isoformat(),
        "status": compute_status(due, today),
        "days_overdue": -days_delta if days_delta < 0 else 0,
        "invoice_number": inv.get("DocNumber", ""),
    }


def softr_headers(api_key):
    return {
        "Softr-Api-Key": api_key.strip(),
        "Content-Type": "application/json",
    }


def fetch_all_softr_records(database_id, table_id, api_key):
    records, offset, limit = [], 0, 100
    while True:
        r = httpx.get(
            f"{SOFTR_API_BASE}/databases/{database_id}/tables/{table_id}/records",
            headers=softr_headers(api_key),
            params={"limit": limit, "offset": offset, "fieldNames": "true"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("data", [])
        records.extend(batch)
        total = data.get("metadata", {}).get("total", len(records))
        offset += limit
        if offset >= total or not batch:
            break
    return records


def softr_create_record(database_id, table_id, api_key, fields):
    r = httpx.post(
        f"{SOFTR_API_BASE}/databases/{database_id}/tables/{table_id}/records",
        headers=softr_headers(api_key),
        params={"fieldNames": "true"},
        json={"fields": fields},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def softr_update_record(database_id, table_id, api_key, record_id, fields):
    r = httpx.patch(
        f"{SOFTR_API_BASE}/databases/{database_id}/tables/{table_id}/records/{record_id}",
        headers=softr_headers(api_key),
        params={"fieldNames": "true"},
        json={"fields": fields},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def softr_delete_record(database_id, table_id, api_key, record_id):
    r = httpx.delete(
        f"{SOFTR_API_BASE}/databases/{database_id}/tables/{table_id}/records/{record_id}",
        headers=softr_headers(api_key),
        timeout=30,
    )
    r.raise_for_status()


def sync_to_softr(database_id, table_id, api_key, invoices):
    existing_records = fetch_all_softr_records(database_id, table_id, api_key)
    existing_by_invnum = {
        rec["fields"].get("invoice_number"): rec
        for rec in existing_records
        if rec.get("fields", {}).get("invoice_number")
    }

    new_map = {inv["DocNumber"]: inv for inv in invoices}
    new_nums = set(new_map)
    existing_nums = set(existing_by_invnum)

    to_create = new_nums - existing_nums
    to_update = new_nums & existing_nums
    to_delete = existing_nums - new_nums

    created, updated, deleted = 0, 0, 0

    for num in to_create:
        softr_create_record(database_id, table_id, api_key, format_fields(new_map[num]))
        created += 1

    for num in to_update:
        record_id = existing_by_invnum[num]["id"]
        softr_update_record(database_id, table_id, api_key, record_id, format_fields(new_map[num]))
        updated += 1

    for num in to_delete:
        record_id = existing_by_invnum[num]["id"]
        softr_delete_record(database_id, table_id, api_key, record_id)
        deleted += 1

    return created, updated, deleted


def main():
    print(f"🚀 AR sync (Softr) — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    refresh_token = get_qbo_refresh_token_from_github(os.environ["GH_PAT"])
    access_token = get_qbo_access_token(
        os.environ["QBO_CLIENT_ID"],
        os.environ["QBO_CLIENT_SECRET"],
        refresh_token,
    )
    realm_id = os.environ["QBO_REALM_ID"]

    all_invoices = fetch_unpaid_invoices(realm_id, access_token)
    customer_map = fetch_customer_map(realm_id, access_token)
    enrich_invoices(all_invoices, customer_map)
    filtered = filter_and_sort(all_invoices)
    print(f"✅ {len(all_invoices)} unpaid, {len(filtered)} after filter")

    created, updated, deleted = sync_to_softr(
        os.environ["SOFTR_DATABASE_ID"],
        os.environ["SOFTR_TABLE_ID"],
        os.environ["SOFTR_API_KEY"],
        filtered,
    )
    print(f"✅ Done — {created} created, {updated} updated, {deleted} deleted")


if __name__ == "__main__":
    main()