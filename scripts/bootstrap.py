"""Initialize KEEL's own work item and optionally collect real evidence.

Preserves existing config/contracts. Never invents reviews or deployment records.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdlc.engine import Harness
from sdlc.schema import default_config, draft
from sdlc.workspace import write_json


def configuration():
    config = default_config()
    config["source_roots"] = ["sdlc", "tests", "scripts", "examples", "schemas", "docs", "templates", ".github", "README.md", "AGENTS.md", "pyproject.toml", "Makefile", "LICENSE"]
    config["checks"] = {
        "unit": {"argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"], "kind": "test", "timeout_seconds": 120, "cwd": ".", "env": {}},
        "schemas": {"argv": ["python3", "scripts/check_schemas.py"], "kind": "lint", "timeout_seconds": 30, "cwd": ".", "env": {}},
        "demo": {"argv": ["python3", "-m", "examples.demo"], "kind": "test", "timeout_seconds": 60, "cwd": ".", "env": {}},
        "health": {"argv": ["python3", "-m", "sdlc", "--version"], "kind": "observe", "timeout_seconds": 10, "cwd": ".", "env": {}},
    }
    return config


def contract():
    manifest = draft("keel-core", "あいまいな要求と不確実な前提を、証拠・予算・復旧可能な状態で扱うローカルSDLCハーネスを届ける")
    manifest.update(owner="repository-maintainer", risk="medium")
    manifest["scope"] = {
        "include": ["モデルに依存しないCLI", "契約と不確実性", "入力に結び付けた証拠", "予算付きPOSIX実行", "リリースの引き渡しと観測", "日本語の運用手順"],
        "exclude": ["モデルAPIの呼出し", "本番への自動配置", "認証済み承認サービス", "OSサンドボックス", "常駐監視", "分散実行基盤"],
        "constraints": ["実行時はPython標準ライブラリのみ", "検証環境はLinux / Python 3.12", "別環境では検証してから導入", "最新の失敗を古い成功で隠さない"],
    }
    specifications = [
        ("契約と依存グラフを検証する", ["型・参照・重複・循環を拒否する", "不足した条件では状態を進めず理由を返す"], ["unit", "schemas"]),
        ("不確実性を証拠で扱う", ["高影響の未決定事項を受容だけで通さない", "要求確認と技術証拠を区別する", "反証と期限付きの受容を扱う"], ["unit", "demo"]),
        ("入力の変化で証拠を失効させる", ["コード・設定・環境・成果物・ログの変化を検出する", "最新の失敗と期限切れを検出する"], ["unit"]),
        ("チェックを予算内で実行して中断を記録する", ["時間・出力・回数・試行数を制限する", "通常の子プロセスを停止する", "クラッシュした予約の消費を保持する"], ["unit"]),
        ("状態と引き渡しの一貫性を確認できる", ["同一世代からの同時更新は一方だけ成功する", "履歴の再生と破損検出を行う", "成果物・証拠・回答・配置記録を照合する", "観測後に完了する"], ["unit", "demo"]),
    ]
    for index, (statement, acceptance, checks) in enumerate(specifications, 1):
        manifest["requirements"].append({"id": f"R{index}", "statement": statement, "acceptance": acceptance, "checks": checks, "priority": "must"})
        manifest["slices"].append({"id": f"S{index}", "title": statement, "requirements": [f"R{index}"], "checks": checks,
                                   "depends_on": ["S1"] if index > 1 else [], "done_when": "対応する受け入れ条件と回帰テストが通る",
                                   "rollback": "実行中の処理を停止し、前の配布版と対応する状態・証拠のバックアップへ戻す"})
    manifest["decisions"] = [{"id": "D1", "question": "制御と権限の境界をどこに置くか", "choice": "標準ライブラリのCLI、SQLite、外部配置へのパケット接続",
                              "alternatives": ["プロンプトだけで制御する", "認証・配置を含む外部ワークフローサービスに統合する"],
                              "rationale": "ローカルで再現できる機械的な条件と、導入先の判断・認証・隔離を明確に分ける",
                              "reversibility": "costly", "rollback": "書き手を停止し、検証済みの配布版と状態バックアップを新しいディレクトリへ復元する", "uncertainties": []}]
    manifest["release"] = {
        "strategy": "検証したソースをローカル成果物として引き渡す。外部配置は導入先で行う",
        "rollback": "前の配布版と対応する状態・証拠バックアップへ戻す",
        "abort_when": ["ゲート未達", "履歴整合性の破損", "実行監督の境界で回帰が発生"],
        "observe_checks": ["health"], "artifacts": [f"sdlc/{name}.py" for name in ("__init__", "__main__", "cli", "engine", "policy", "runner", "schema", "store", "workspace")],
        "irreversible": False, "observation_window_seconds": 60,
    }
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Run verification and advance to verify; review and deployment remain explicit")
    args = parser.parse_args()
    if not (ROOT / ".sdlc/state.db").exists():
        Harness.initialize(ROOT, adopt=(ROOT / ".sdlc").exists())
    harness = Harness(ROOT)
    try:
        if not harness.config()["checks"]:
            write_json(ROOT / ".sdlc/config.json", configuration())
        if "keel-core" not in {state["id"] for state in harness.store.all()}:
            path = harness.manifest_path("keel-core")
            if not path.exists():
                write_json(path, contract(), exclusive=True)
            harness.import_change(path.relative_to(ROOT).as_posix())
        if args.verify:
            if harness.store.get("keel-core")["phase"] in {"release", "observe", "closed"}:
                raise SystemExit("Use reopen to return to verification, or create a new change for further work")
            for name in ("unit", "schemas", "demo"):
                result = harness.run("keel-core", name)
                print(json.dumps({"check": name, "status": result["status"], "evidence": result["id"]}), flush=True)
                if result["status"] != "pass":
                    raise SystemExit(f"Verification stopped. Inspect {result['artifact']}")
            while harness.store.get("keel-core")["phase"] != "verify":
                harness.advance("keel-core")
        status = harness.status("keel-core")
        print(json.dumps({"id": "keel-core", "phase": status["phase"], "revision": status["revision"], "next_actions": status["next_actions"]}, ensure_ascii=False, indent=2))
    finally:
        harness.close()


if __name__ == "__main__":
    main()
