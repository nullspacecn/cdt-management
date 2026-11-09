#!/usr/bin/env python3
"""每天固定时刻换班（daily24）+ DDNS 前置 + Kuma 心跳的测试（fork 新增）。

跑法：python scripts/test_daily24.py

锁死的规则：
  1. daily24：每天 switch_time（默认 03:00 北京时间）换班一次、每人 24 小时，
     成员按 position 顺序逐日交替；anchor_at 所在日期的 switch_time 由
     members[0] 当班。纯日历计算，不随容器重启/建组时间漂移。
  2. DDNS 前置：换班时先把域名指到新当班机、确认同步成功再停旧机，
     事件里 DDNS 只记一行、且排在停机之前。
  3. Kuma 心跳：默认关闭；开启后先验本机 /api/health，通过才推，
     健康检查失败不推、不崩；push URL 不写进日志（本测试只验行为）。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-daily24-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, dns, notify, repo, scheduler, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


def _now_dt(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, TZ)


def _make_server(label: str, instance_id: str) -> int:
    return repo.upsert_server(
        {
            "label": label,
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "site": "international",
            "instance_id": instance_id,
            "quota_gb": 200,
            "threshold_gb": 188,
        }
    )


class Daily24Duty(unittest.TestCase):
    """纯排班计算：anchor 2026-11-09 03:00（A 当班），之后每天 03:00 交替。"""

    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()
        cls.a_id = _make_server("机器A", "i-d24-aaa")
        cls.b_id = _make_server("机器B", "i-d24-bbb")
        cls.group_id = repo.upsert_group(
            {
                "name": "日轮组",
                "enabled": True,
                "failover_enabled": True,
                "cycle": "daily24",
                "switch_time": "03:00",
                "members": [
                    {"server_id": cls.a_id, "hours": 24},
                    {"server_id": cls.b_id, "hours": 24},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(_ts(2026, 11, 9, 3, 0)), cls.group_id),
        )

    def _duty_label(self, ts: float) -> str:
        group = repo.get_group(self.group_id) or {}
        picked = shift.duty_at(group, ts)
        self.assertIsNotNone(picked, f"无当班：{ts}")
        assert picked is not None
        return str(picked[0].get("label") or "")

    def test_duty_alternates_daily_at_0300(self) -> None:
        self.assertEqual(self._duty_label(_ts(2026, 11, 9, 3, 0)), "机器A")
        self.assertEqual(self._duty_label(_ts(2026, 11, 10, 2, 59)), "机器A")
        self.assertEqual(self._duty_label(_ts(2026, 11, 10, 3, 0)), "机器B")
        self.assertEqual(self._duty_label(_ts(2026, 11, 10, 12, 0)), "机器B")
        self.assertEqual(self._duty_label(_ts(2026, 11, 11, 2, 59)), "机器B")
        self.assertEqual(self._duty_label(_ts(2026, 11, 11, 3, 0)), "机器A")
        self.assertEqual(self._duty_label(_ts(2026, 11, 12, 3, 0)), "机器B")

    def test_next_switch_at(self) -> None:
        group = repo.get_group(self.group_id) or {}
        self.assertEqual(shift.next_switch_at(group, _ts(2026, 11, 10, 2, 32)), _ts(2026, 11, 10, 3, 0))
        self.assertEqual(shift.next_switch_at(group, _ts(2026, 11, 10, 3, 0)), _ts(2026, 11, 11, 3, 0))
        self.assertEqual(shift.next_switch_at(group, _ts(2026, 11, 10, 4, 0)), _ts(2026, 11, 11, 3, 0))

    def test_group_validation(self) -> None:
        with self.assertRaises(ValueError):
            repo.upsert_group(
                {
                    "name": "坏时刻组",
                    "enabled": True,
                    "cycle": "daily24",
                    "switch_time": "25:00",
                    "members": [
                        {"server_id": self.a_id, "hours": 24},
                        {"server_id": self.b_id, "hours": 24},
                    ],
                }
            )


class Daily24Reconcile(unittest.TestCase):
    """daily24 换班全流程：2026-11-10 02:50（A 当班），A 未起、B 在跑，
    DDNS 前置——域名先指到 A，确认同步后再停 B，事件里 DDNS 只记一行且在停机前。"""

    def setUp(self) -> None:
        db.init_db()
        for table in ("events", "flags", "shift_runs", "shift_members", "shift_groups", "servers"):
            db.execute(f"DELETE FROM {table}")
        self.a_id = _make_server("日轮A", "i-d24r-aaa")
        self.b_id = _make_server("日轮B", "i-d24r-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "日轮换班组",
                "enabled": True,
                "failover_enabled": True,
                "cycle": "daily24",
                "switch_time": "03:00",
                "members": [
                    {"server_id": self.a_id, "hours": 24},
                    {"server_id": self.b_id, "hours": 24},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(_ts(2026, 11, 9, 3, 0)), self.group_id),
        )
        repo.set_settings({"ddns_enabled": "1"})
        repo.ensure_dns_record("hk.cscec.ac.cn")
        repo.bind_dns_record("hk.cscec.ac.cn", dns.TARGET_GROUP, self.group_id)
        aliyun.start_instance(repo.get_server(self.b_id, decrypt_secret=True))
        self.now_ts = _ts(2026, 11, 10, 2, 50)

    def _reconcile(self) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(self.group_id) or {},
            repo.get_settings(),
            _now_dt(self.now_ts),
            self.now_ts,
        )

    def test_ddns_before_stop(self) -> None:
        summary: dict = {}
        for _ in range(5):
            summary = self._reconcile()
        self.assertTrue(summary.get("ready"), summary)
        self.assertEqual((repo.get_server(self.a_id) or {}).get("instance_status"), "Running")
        self.assertEqual((repo.get_server(self.b_id) or {}).get("instance_status"), "Stopped")
        events = repo.list_events(limit=50, group_id=self.group_id)
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        texts = [step["text"] for step in (events[0].get("progress") or [])]
        self.assertEqual(sum(t.startswith("DDNS：") for t in texts), 1, texts)
        self.assertLess(
            next(i for i, t in enumerate(texts) if t.startswith("DDNS：")),
            next(i for i, t in enumerate(texts) if "已下发停机" in t),
            texts,
        )
        record = repo.get_dns_record("hk.cscec.ac.cn") or {}
        self.assertTrue(str(record.get("last_status") or "").startswith("✅"), record)


class KumaHeartbeat(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        scheduler._last_kuma_push = 0.0
        scheduler._last_kuma_attempt = 0.0

    def test_disabled_is_noop(self) -> None:
        repo.set_settings({"kuma_enabled": "0", "kuma_push_url": "https://example.invalid/push"})
        with mock.patch("app.scheduler.requests.get") as fake_get:
            scheduler._maybe_kuma_heartbeat(now=time.time())
            fake_get.assert_not_called()

    def test_health_failure_does_not_push(self) -> None:
        repo.set_settings({"kuma_enabled": "1", "kuma_push_url": "https://example.invalid/push"})
        with mock.patch("app.scheduler.requests.get") as fake_get:
            # 本机没有跑 HTTP 服务（测试环境 8080 无监听），健康检查失败 → 不推
            scheduler._maybe_kuma_heartbeat(now=time.time())
            fake_get.assert_not_called()

    def test_push_after_healthy_check(self) -> None:
        repo.set_settings({"kuma_enabled": "1", "kuma_push_url": "https://example.invalid/push"})
        fake_resp = mock.Mock()
        fake_resp.status = 200
        fake_http = mock.Mock()
        fake_http.__enter__ = mock.Mock(return_value=fake_resp)
        fake_http.__exit__ = mock.Mock(return_value=False)
        with (
            mock.patch("app.scheduler.urllib.request.urlopen", return_value=fake_http),
            mock.patch("app.scheduler.requests.get") as fake_get,
        ):
            fake_get.return_value = mock.Mock()
            scheduler._maybe_kuma_heartbeat(now=1_000_000.0)
            fake_get.assert_called_once()
            self.assertEqual(scheduler._last_kuma_push, 1_000_000.0)
            # 间隔内不再重复推
            fake_get.reset_mock()
            scheduler._maybe_kuma_heartbeat(now=1_000_100.0)
            fake_get.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
