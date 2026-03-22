"""Tests for analyze.py — trade analysis and reporting."""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from analyze import load_trades, print_summary, wipe_paper, list_days


class TestLoadTrades:
    def test_loads_daily_files(self, tmp_path):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        f = mode_dir / "trades_2026-03-21.jsonl"
        f.write_text(json.dumps({"outcome": "WIN", "pnl": 100, "stake": 500}) + "\n")

        # Monkey-patch DATA_DIR
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            trades = load_trades("paper")
            assert len(trades) == 1
            assert trades[0]["pnl"] == 100
        finally:
            analyze.DATA_DIR = orig

    def test_loads_multiple_days(self, tmp_path):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-20.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 50}) + "\n")
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "LOSS", "pnl": -100}) + "\n")

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            trades = load_trades("paper")
            assert len(trades) == 2
        finally:
            analyze.DATA_DIR = orig

    def test_date_filter(self, tmp_path):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-20.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 50}) + "\n")
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "LOSS", "pnl": -100}) + "\n")

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            trades = load_trades("paper", date_from="2026-03-21", date_to="2026-03-21")
            assert len(trades) == 1
            assert trades[0]["pnl"] == -100
        finally:
            analyze.DATA_DIR = orig

    def test_empty_dir(self, tmp_path):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            trades = load_trades("paper")
            assert trades == []
        finally:
            analyze.DATA_DIR = orig

    def test_missing_dir(self, tmp_path):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            trades = load_trades("nonexistent")
            assert trades == []
        finally:
            analyze.DATA_DIR = orig


class TestPrintSummary:
    def test_no_trades(self, capsys):
        print_summary([], "test")
        out = capsys.readouterr().out
        assert "No trades" in out

    def test_with_trades(self, capsys):
        trades = [
            {"outcome": "WIN", "pnl": 100, "stake": 500, "edge_pct": 3.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "A vs B",
             "side": "NO", "league": "EPL", "trade_type": "STANDARD"},
            {"outcome": "LOSS", "pnl": -200, "stake": 800, "edge_pct": 2.0,
             "timestamp": "2026-03-21T21:00:00Z", "event": "C vs D",
             "side": "YES", "league": "La Liga", "trade_type": "PRE-MATCH"},
        ]
        print_summary(trades, "paper")
        out = capsys.readouterr().out
        assert "2" in out
        assert "EPL" in out

    def test_edge_buckets(self, capsys):
        """Should show edge range breakdown including all buckets."""
        trades = [
            {"outcome": "WIN", "pnl": 10, "stake": 100, "edge_pct": 1.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "A", "side": "NO"},
            {"outcome": "WIN", "pnl": 50, "stake": 200, "edge_pct": 3.5,
             "timestamp": "2026-03-21T20:00:00Z", "event": "B", "side": "NO"},
            {"outcome": "LOSS", "pnl": -100, "stake": 300, "edge_pct": 7.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "C", "side": "YES"},
            {"outcome": "WIN", "pnl": 200, "stake": 400, "edge_pct": 12.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "D", "side": "YES"},
        ]
        print_summary(trades, "paper")
        out = capsys.readouterr().out
        assert "0-2%" in out
        assert "2-5%" in out
        assert "5-10%" in out
        assert "10%+" in out

    def test_pnl_curve(self, capsys):
        """Should show P&L curve when >= 3 trades."""
        trades = [
            {"outcome": "WIN", "pnl": 100, "stake": 500, "edge_pct": 3.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "A", "side": "NO"},
            {"outcome": "LOSS", "pnl": -50, "stake": 200, "edge_pct": 2.0,
             "timestamp": "2026-03-21T20:30:00Z", "event": "B", "side": "YES"},
            {"outcome": "WIN", "pnl": 75, "stake": 300, "edge_pct": 4.0,
             "timestamp": "2026-03-21T21:00:00Z", "event": "C", "side": "NO"},
        ]
        print_summary(trades, "paper")
        out = capsys.readouterr().out
        assert "P&L CURVE" in out

    def test_single_league_no_table(self, capsys):
        """With only unknown leagues, should still work."""
        trades = [
            {"outcome": "WIN", "pnl": 50, "stake": 200, "edge_pct": 3.0,
             "timestamp": "2026-03-21T20:00:00Z", "event": "A", "side": "NO"},
        ]
        print_summary(trades, "test")
        # Should not crash


class TestWipePaper:
    def test_wipes_files(self, tmp_path):
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        (paper_dir / "trades_2026-03-21.jsonl").write_text("{}\n")
        (paper_dir / "state.json").write_text("{}\n")

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            wipe_paper()
            assert len(list(paper_dir.iterdir())) == 0
        finally:
            analyze.DATA_DIR = orig

    def test_wipe_nonexistent(self, tmp_path, capsys):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            wipe_paper()
            out = capsys.readouterr().out
            assert "No paper data" in out
        finally:
            analyze.DATA_DIR = orig


class TestListDays:
    def test_lists_files(self, tmp_path, capsys):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 100}) + "\n")

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            list_days("paper")
            out = capsys.readouterr().out
            assert "2026-03-21" in out
            assert "1 trades" in out
        finally:
            analyze.DATA_DIR = orig

    def test_missing_dir(self, tmp_path, capsys):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            list_days("nonexistent")
            out = capsys.readouterr().out
            assert "No" in out
        finally:
            analyze.DATA_DIR = orig

    def test_empty_dir(self, tmp_path, capsys):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()

        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        try:
            list_days("paper")
            out = capsys.readouterr().out
            assert "No" in out
        finally:
            analyze.DATA_DIR = orig


class TestMainFunction:
    """Test the analyze.py main() with different CLI args."""

    def test_paper_default(self, tmp_path, capsys, monkeypatch):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 100, "stake": 500, "edge_pct": 3.0,
                         "timestamp": "2026-03-21T20:00:00Z", "event": "A vs B",
                         "side": "NO"}) + "\n")
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "PAPER" in out
        finally:
            analyze.DATA_DIR = orig

    def test_live_flag(self, tmp_path, capsys, monkeypatch):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py", "--live"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "No trades" in out
        finally:
            analyze.DATA_DIR = orig

    def test_all_flag(self, tmp_path, capsys, monkeypatch):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py", "--all"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "No trades" in out
        finally:
            analyze.DATA_DIR = orig

    def test_days_flag(self, tmp_path, capsys, monkeypatch):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 50}) + "\n")
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py", "--days"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "2026-03-21" in out
        finally:
            analyze.DATA_DIR = orig

    def test_date_filter(self, tmp_path, capsys, monkeypatch):
        mode_dir = tmp_path / "paper"
        mode_dir.mkdir()
        (mode_dir / "trades_2026-03-21.jsonl").write_text(
            json.dumps({"outcome": "WIN", "pnl": 50, "stake": 200, "edge_pct": 3.0,
                         "timestamp": "2026-03-21T20:00:00Z", "event": "X", "side": "NO"}) + "\n")
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py", "--date", "2026-03-21"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "2026-03-21" in out
        finally:
            analyze.DATA_DIR = orig

    def test_today_flag(self, tmp_path, capsys, monkeypatch):
        import analyze
        orig = analyze.DATA_DIR
        analyze.DATA_DIR = tmp_path
        monkeypatch.setattr("sys.argv", ["analyze.py", "--today"])
        try:
            from analyze import main
            main()
            out = capsys.readouterr().out
            assert "No trades" in out
        finally:
            analyze.DATA_DIR = orig
