"""What the staging database does when an order file changes.

An order file is rewritten in normal use -- a fuller re-capture replaces a truncated one,
a retrim shortens one, a resegment rebuilds one. The conversation id used to carry the
file's content digest, so every one of those minted a SECOND conversation while the first
stayed staged forever, and the export wrote both: one order file, two rows, same date and
customer, different addresses. It reads as the model inventing an order, which is the worst
possible way for a bookkeeping bug to present.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lavabo.config import Config                                  # noqa: E402
from lavabo.connectors.zalo_export import ZaloExportConnector     # noqa: E402
from lavabo.models import Source                                  # noqa: E402
from lavabo.store import Store                                    # noqa: E402

FIRST = ("15/8 đơn 1 - Meloxicam\n1 tủ BC52\n"
         "Xóm 3 thôn vạn đồn, thái thuỵ, TB\n0367002126\nTổng 29tr")
REWRITTEN = ("15/8 đơn 1 - Meloxicam\n1 tủ BC52\n"
             "Tổ 8 KP suối lớn, phú quốc, kiên giang\n0939813206\nTổng 29tr")


class OneFileOneConversation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.inbox = base / "zalo"
        self.inbox.mkdir(parents=True)
        self.cfg = Config()
        self.cfg.zalo.inbox_dir = self.inbox
        self.store = Store(base / "s.db")
        self.order = self.inbox / "15-8 đơn 1 - Meloxicam.txt"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ingest(self):
        """The same sequence cmd_ingest runs."""
        done = set(json.loads(self.store.get_state("zalo:files") or "[]"))
        staged = {c.conversation_id for c in self.store.conversations(source=Source.ZALO)}
        conn = ZaloExportConnector(self.cfg.zalo, processed=done, staged=staged)
        for conv in conn.fetch():
            self.store.upsert_conversation(conv, replace_messages=True)
        self.store.set_state("zalo:files", json.dumps(sorted(done | conn.seen_hashes)))
        return self.store.prune_conversations(Source.ZALO, conn.current_stems(),
                                              identify=conn.file_stem)

    def test_rewriting_a_file_does_not_mint_a_second_conversation(self):
        self.order.write_text(FIRST, encoding="utf-8")
        self._ingest()
        self.order.write_text(REWRITTEN, encoding="utf-8")
        self._ingest()
        self.assertEqual(self.store.stats()["conversations"], 1)

    def test_the_id_does_not_move_when_the_content_does(self):
        self.order.write_text(FIRST, encoding="utf-8")
        self._ingest()
        first = self.store.conversations(source=Source.ZALO)[0].conversation_id
        self.order.write_text(REWRITTEN, encoding="utf-8")
        self._ingest()
        self.assertEqual(self.store.conversations(source=Source.ZALO)[0].conversation_id,
                         first)

    def test_lines_a_file_lost_are_not_left_staged(self):
        """A retrim or a resegment makes a file SHORTER. Inserting without deleting left
        the dropped lines behind, so the order kept an address it no longer had."""
        self.order.write_text(FIRST, encoding="utf-8")
        self._ingest()
        self.order.write_text(REWRITTEN, encoding="utf-8")
        self._ingest()
        staged = " ".join(m.text for m in self.store.conversations(source=Source.ZALO)[0].messages)
        self.assertIn("phú quốc", staged)
        self.assertNotIn("thái thuỵ", staged)

    def _db_path(self):
        return self.store.conn.execute("PRAGMA database_list").fetchone()[2]

    def _write_old_scheme_rows(self, digests, *, with_extraction=True):
        """A database as an earlier version left it: ids carrying the content digest."""
        path = self._db_path()
        self.store.close()
        with sqlite3.connect(path) as raw:
            for digest in digests:
                cid = f"zalo:15-8 đơn 1 - Meloxicam:{digest}"
                raw.execute(
                    "INSERT INTO conversations (source, conversation_id, customer_name,"
                    " raw, ingested_at) VALUES (?,?,?,?,?)",
                    ("zalo", cid, "Meloxicam", "{}", "now"))
                if with_extraction:
                    raw.execute(
                        "INSERT INTO extractions (source, conversation_id, content_hash,"
                        " schema_version, schema_hash, prompt_version, model, values_json,"
                        " extracted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("zalo", cid, "h", 1, "s", 3, "m", "{}", "now"))
        self.store = Store(Path(path))          # reopening runs the migration
        return path

    def test_old_ids_are_migrated_and_keep_their_extractions(self):
        """Dropping these instead charged a whole month of re-extraction to fix a
        bookkeeping mistake. The cached answers must survive the rename."""
        self.order.write_text(REWRITTEN, encoding="utf-8")
        self._write_old_scheme_rows(["906d8f178140e66a"])
        self.assertEqual(self.store.stats()["conversations"], 1)
        self.assertEqual(self.store.stats()["extractions"], 1)
        self.assertEqual(self.store.conversations(source=Source.ZALO)[0].conversation_id,
                         "zalo:15-8 đơn 1 - Meloxicam")

    def test_two_old_rows_for_one_file_collapse_to_one(self):
        self.order.write_text(REWRITTEN, encoding="utf-8")
        self._write_old_scheme_rows(["906d8f178140e66a", "c52601397ea8deb3"])
        self.assertEqual(self.store.stats()["conversations"], 1)
        self.assertEqual(self._ingest(), 0, "nothing should look orphaned afterwards")

    def test_an_unmigrated_old_id_is_not_treated_as_orphaned(self):
        """The defect that emptied a staged month: the prune compared whole ids, so when
        the scheme changed every existing row looked like a file that had been deleted."""
        self.order.write_text(REWRITTEN, encoding="utf-8")
        stem = ZaloExportConnector.file_stem(
            "zalo:15-8 đơn 1 - Meloxicam:906d8f178140e66a")
        self.assertEqual(stem, "15-8 đơn 1 - Meloxicam")
        self.assertEqual(
            self.store.prune_conversations(Source.ZALO, {stem},
                                           identify=ZaloExportConnector.file_stem), 0)

    def test_a_file_with_no_row_is_re_staged_even_if_marked_ingested(self):
        """Recovery path. "processed" is an optimisation and must never be the reason a
        file on disk has nothing staged behind it."""
        self.order.write_text(FIRST, encoding="utf-8")
        self._ingest()
        self.assertEqual(self.store.stats()["conversations"], 1)

        # Wipe the staging table but leave the file marked as already ingested.
        with self.store.tx() as c:
            c.execute("DELETE FROM conversations WHERE source='zalo'")
        self.assertEqual(self.store.stats()["conversations"], 0)

        self._ingest()
        self.assertEqual(self.store.stats()["conversations"], 1)

    def test_a_deleted_file_stops_being_exported(self):
        self.order.write_text(FIRST, encoding="utf-8")
        (self.inbox / "16-8 đơn 2 - Khác.txt").write_text(
            "16/8 đơn 2 - Khác\n1 lavabo\nTổng 2tr", encoding="utf-8")
        self._ingest()
        self.assertEqual(self.store.stats()["conversations"], 2)
        self.order.unlink()
        self._ingest()
        self.assertEqual(self.store.stats()["conversations"], 1)

    def test_an_empty_inbox_never_wipes_the_staging_table(self):
        """A mistyped inbox_dir must not be how a month of orders disappears."""
        self.order.write_text(FIRST, encoding="utf-8")
        self._ingest()
        self.assertEqual(
            self.store.prune_conversations(Source.ZALO, set(),
                                           identify=ZaloExportConnector.file_stem), 0)
        self.assertEqual(self.store.stats()["conversations"], 1)

    def test_meta_style_paging_is_left_alone(self):
        """Meta pages history in incrementally, so replacing its messages would delete
        what has not been fetched yet. It stays off unless asked for."""
        from lavabo.models import Conversation, Direction, Message

        def page(ids):
            conv = Conversation(conversation_id="m:1", source=Source.MESSENGER,
                                customer_name="X", messages=[])
            conv.messages = [Message(source=Source.MESSENGER, conversation_id="m:1",
                                     message_id=f"m:1:{i}", sent_at=None,
                                     direction=Direction.INBOUND, sequence=i,
                                     text=f"line {i}")
                             for i in ids]
            return conv

        self.store.upsert_conversation(page([1, 2]))
        self.store.upsert_conversation(page([3]))
        self.assertEqual(len(self.store.conversations(source=Source.MESSENGER)[0].messages), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
