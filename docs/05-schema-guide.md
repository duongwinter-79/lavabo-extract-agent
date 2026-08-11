# Defining your columns

When your requirements arrive, they go in **one file**: `config/schema.yaml`. No code
changes. That file generates both the JSON schema the model must fill and the Excel header
row, in the order you list them.

---

## Anatomy of a column

```yaml
- name: order_value_vnd        # Excel header + JSON key. snake_case, no spaces.
  type: number                 # string | number | integer | boolean | date | array
  required: true               # only affects `lavabo verify` null-rate warnings
  enum: [a, b, c]              # optional; the model can only pick from these (or null)
  description: >               # THE MOST IMPORTANT FIELD
    Final agreed order value in VND as a plain number (e.g. 2500000 for "2tr5").
    Null unless a specific amount was agreed.
```

## The description is the prompt

Extraction quality is almost entirely determined by the descriptions. A vague one produces
confident nonsense.

| Bad | Good |
|---|---|
| `Customer name` | `The customer's name as they gave it in the conversation. Prefer a name they typed over the account display name. Null if never stated.` |
| `Price` | `Final agreed order value in VND as a plain number (e.g. 2500000 for "2tr5"). Null unless a specific amount was agreed. If several prices were discussed, use the last one both sides agreed to.` |
| `Status` | Use an `enum` instead — free text status columns are unusable for filtering. |

Rules that pay for themselves:

1. **Say what "not present" looks like.** Every description should end with when to return
   null. Otherwise the model invents.
2. **Give the format, in the description.** "digits only, no country code",
   "YYYY-MM-DD", "plain number, no separators".
3. **Use `enum` for anything you'll filter or pivot on.** Free text will come back as
   fifteen spellings of the same thing.
4. **Say which language you want.** These conversations are Vietnamese/English mixed. Be
   explicit per column: keep source language, or normalize to one.
5. **One fact per column.** "Product and price" as one column is two columns.
6. **Resolve contradictions in the description.** "If several were discussed, use the last
   one" removes a whole class of inconsistency.

## Derived columns: don't ask the model

If a value is computable — message count, first/last date, response time, source platform —
compute it in `load/excel.py` rather than asking the LLM. It's free, instant, and always
right. Ask the model only for things that require reading and judgement.

## Workflow when adding or changing a column

```bash
# 1. Edit config/schema.yaml, bump schema_version

# 2. See the exact prompt, spend nothing
lavabo extract --limit 3 --dry-run

# 3. Try it on a few real conversations
lavabo extract --limit 10
lavabo load --out data/out/sample.xlsx

# 4. Open the workbook. Amber cells are nulls. Check the Sources sheet for anything
#    that looks wrong, and sharpen the description rather than the prompt.

# 5. Happy? Run the lot.
lavabo extract && lavabo load --out data/out/report.xlsx && lavabo verify
```

Bumping `schema_version` invalidates the extraction cache — that's intended. The model sees
all columns at once and they influence each other, so a partial re-extraction would mix
results from two different schemas in one row.

## Cost

Roughly `(transcript_chars / 4)` input tokens per conversation, once per extraction. A
1,000-conversation backfill at ~3k tokens each is ~3M input tokens. Two things keep this in
hand:

- Use a cheaper model (`claude-sonnet-5`) for bulk backfill, the strongest model for
  spot-checks; `--dry-run` prints the estimate before you commit.
- The cache means the second run of an unchanged schema costs nothing.
