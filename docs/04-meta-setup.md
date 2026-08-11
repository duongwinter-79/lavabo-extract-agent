# Meta setup

Goal: a **Page access token** that can read `/{PAGE_ID}/conversations`.

The blocker on this path is never the code — it's **Advanced Access**, which requires App
Review and Business Verification. Start that process on day one; it is the long pole.

---

## 1. App and permissions

1. [developers.facebook.com](https://developers.facebook.com) → **My Apps** → **Create App**
   → type **Business**.
2. Add the **Messenger** product (and **Instagram** if IG DMs are in scope).
3. Link your Page under Messenger → Settings → Access Tokens.
4. Request these permissions:

| Permission | Why |
|---|---|
| `pages_messaging` | Read/send Page messages |
| `pages_read_engagement` | Read Page content |
| `pages_manage_metadata` | Required alongside the above for conversations |
| `business_management` | Only if the Page is managed via Business Manager |
| `instagram_basic` + `instagram_manage_messages` | Instagram DMs only |

## 2. Standard vs Advanced Access — the important part

- **Standard Access** (immediate) only returns conversations with people who have a **role on
  your app or Page** — admins, developers, testers. You can build and test the whole pipeline
  with it by messaging your Page from a test account added under App Roles.
- **Advanced Access** (App Review + Business Verification) is required to see **real
  customers**. Without it, `/conversations` returns an empty or near-empty list and the code
  looks broken when it isn't.

Business Verification wants legal business documents and takes days to weeks. **Start it
before you need it.**

## 3. Get a long-lived Page token

Short-lived tokens from Graph Explorer expire in ~1 hour. Exchange for a long-lived one:

```bash
# 1. Long-lived USER token (~60 days)
curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
  -d grant_type=fb_exchange_token \
  -d client_id=$APP_ID \
  -d client_secret=$APP_SECRET \
  -d fb_exchange_token=$SHORT_LIVED_USER_TOKEN

# 2. PAGE token derived from it — this one does not expire
curl -G "https://graph.facebook.com/v21.0/me/accounts" \
  -d access_token=$LONG_LIVED_USER_TOKEN
```

Put the Page token in `.env` as `META_PAGE_TOKEN`. **Never commit it.**

Find your Page ID: `curl -G "https://graph.facebook.com/v21.0/me/accounts" -d access_token=...`

## 4. Verify

```bash
lavabo check
# OK  messenger: authenticated as Lavabo Store (1234567890)

lavabo ingest --source meta --full
```

Cap the first run while testing:

```yaml
meta:
  max_conversations: 5
```

## 5. Manual check with curl

```bash
curl -G "https://graph.facebook.com/v21.0/$PAGE_ID/conversations" \
  -d platform=messenger \
  -d fields=id,updated_time,message_count,participants \
  -d limit=5 \
  -d access_token=$META_PAGE_TOKEN
```

---

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `(#100) ... nonexisting field` | Wrong node — you queried a user ID, not a Page ID | Use the Page ID from `/me/accounts` |
| `(#200) Requires pages_messaging` | Permission not granted to this token | Re-issue the token after granting |
| Empty `data: []` but the inbox has messages | **Standard Access** | Test with an app-role account, or wait for Advanced Access |
| `(#4)` / `(#17)` / `(#32)` | Rate limited | The connector backs off automatically; lower `page_size` if persistent |
| `(#190)` OAuthException | Token expired or invalidated | Re-run the exchange in step 3 |
| Some old threads missing | Requests-folder threads inactive >30 days are never returned | Documented Meta limitation, not a bug |

---

## Known limits worth designing around

- **Requests folder, inactive >30 days: not returned by the API.** Nothing recovers these
  except manual export.
- **Instagram rate limits are much lower than Messenger's.** Keep `concurrency` low and
  expect IG backfills to take a while.
- **WhatsApp has no history API at all.** The Cloud API delivers by webhook only; you can
  capture from the moment you start listening, never retroactively. If WhatsApp is in scope
  we need a persistent webhook receiver, which is a different architecture — flag it early.

## Fallback while App Review is pending

Meta's **Download Your Information** exports messages as JSON or HTML with a date-range
picker, emailed in ~10–30 minutes. Drop the JSON into `data/inbox/` and it ingests through
the same path as Zalo. Whether this covers the *Page* inbox specifically is worth one test
request — it's the difference between waiting on App Review and starting now.
