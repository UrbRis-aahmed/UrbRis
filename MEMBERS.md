# Membership setup

Members sign up at `/join`. The roll lives in Supabase so it survives redeploys and can be
exported for an insurer's eligibility check.

## 1. Create the table (once, in the Supabase SQL editor)

```sql
create table members (
  id uuid primary key default gen_random_uuid(),
  joined_at timestamptz default now(),
  name text not null,
  email text not null unique,
  postal_code text not null,
  licences text[] default '{}',
  interests text[] default '{}',
  source text,
  consent_text_version text not null,
  consented_at timestamptz not null
);
```

Use the same `SUPABASE_KEY` as the routes table (the service-role key). Keep row-level
security off for this table, or add policies that allow that key to insert and select.

## 2. Render environment variables

| Variable | What it does |
|---|---|
| `MEMBERS_ADMIN_KEY` | Long random string. Required to download the member list. |
| `MEMBER_GOAL` | Optional. Defaults to 5000. |

## 3. Use it

- Sign-up page: `https://urbris.com/join`
- Track where members come from with `?src=`, e.g. `/join?src=rti`, `/join?src=youtube`, `/join?src=dealer-hamilton`.
- Live count (JSON): `/members/count`
- Export for an insurer: `https://urbris.com/members/export.csv?key=<MEMBERS_ADMIN_KEY>`

## 4. Deletion requests

The consent text promises deletion on request at members@urbris.com. Set up email forwarding
for that address in Namecheap. Delete a member in Supabase:
`delete from members where email = 'person@example.com';`
