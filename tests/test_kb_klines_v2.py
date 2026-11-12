import argparse
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import kb_klines


class KbKlinesV2AlignmentTests(unittest.TestCase):
    def test_htf_ladder_maps_hourly_daily_venues_from_1h_to_1d(self) -> None:
        us_stock = kb_klines.AssetSpec("stock", "us", "NVDA")
        hk_stock = kb_klines.AssetSpec("stock", "hk", "00700")
        fx_spot = kb_klines.AssetSpec("forex", "spot", "EURUSD")
        crypto = kb_klines.AssetSpec("binance", "perp", "BTCUSDT")
        gold = kb_klines.AssetSpec("commodity", "spot", "XAUUSD")

        self.assertEqual(kb_klines._htf_for("1h", us_stock), "1d")
        self.assertEqual(kb_klines._htf_for("1h", hk_stock), "1d")
        self.assertEqual(kb_klines._htf_for("1h", fx_spot), "1d")
        self.assertEqual(kb_klines._htf_for("1h", crypto), "4h")
        self.assertEqual(kb_klines._htf_for("1h", gold), "4h")
        self.assertIsNone(kb_klines._htf_for("1d", us_stock))

    def test_mobius_client_attaches_optional_bearer_token_only_when_set(self) -> None:
        client = kb_klines.MobiusClient()
        with patch.dict(os.environ, {"MOBIUS_API_TOKEN": ""}, clear=False):
            headers = client._headers()
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["User-Agent"], "OpenMobius-skill/0.4")

        with patch.dict(os.environ, {"MOBIUS_API_TOKEN": "mq_test_123"}, clear=False):
            authed = client._headers()
        self.assertEqual(authed["Authorization"], "Bearer mq_test_123")

    def test_cmd_fetch_gracefully_handles_unsupported_htf_400(self) -> None:
        args = argparse.Namespace(
            query=None,
            exchange="commodity",
            market="spot",
            symbol="XAUUSD",
            interval="5m",
            limit=10,
            with_htf=True,
            htf_limit=10,
            output=None,
        )
        candles = [[1710000000000, 2900.0, 2910.0, 2895.0, 2905.0, 0.0, 0.0]]

        def fake_klines(exchange, market, symbol, interval, limit=500, **kw):
            if interval == "15m":
                raise kb_klines.MobiusError(400, "unsupported interval", "/api/klines")
            return {"data": candles}

        stdout = io.StringIO()
        with (
            patch.object(kb_klines.MobiusClient, "klines", side_effect=fake_klines),
            redirect_stdout(stdout),
        ):
            rc = kb_klines.cmd_fetch(args)

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["primary"]["count"], 1)
        self.assertIsNone(payload["htf"])

    def test_cmd_chart_skips_volume_panel_for_zero_volume_commodity(self) -> None:
        args = argparse.Namespace(
            query=None,
            exchange="commodity",
            market="spot",
            symbol="XAUUSD",
            interval="1h",
            limit=10,
            value_range_buffer=0.08,
            auto_overlay=False,
            include_mitigated=True,
            include_zones=False,
            include_internal=True,
            max_items=400,
            volume_panel=True,
            output=None,
        )
        zero_vol_rows = [
            [1710000000000, 2900.0, 2910.0, 2895.0, 2905.0, 0.0, 0.0],
            [1710003600000, 2905.0, 2915.0, 2900.0, 2912.0, 0.0, 0.0],
        ]
        stdout = io.StringIO()
        with (
            patch.object(
                kb_klines.MobiusClient,
                "klines",
                return_value={"data": zero_vol_rows},
            ),
            redirect_stdout(stdout),
        ):
            rc = kb_klines.cmd_chart(args)

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        panel_ids = [p["id"] for p in payload["panels"]]
        self.assertEqual(panel_ids, ["main"])


if __name__ == "__main__":
    unittest.main()
