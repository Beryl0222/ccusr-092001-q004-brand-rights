"""赛事品牌全年权益服务入口。

用法：
    python3 service.py --check            # 校验领域词汇并初始化存储
    python3 service.py --port 8000        # 启动 HTTP 服务（默认库文件 ledger.db）
    python3 service.py --db data.db --port 8000
"""

import argparse
import os
import tempfile
from http.server import ThreadingHTTPServer

from brandledger import SERVICE_ID, health  # noqa: F401  （对外保持稳定身份）
from brandledger.api import Api, make_handler
from brandledger.core import Core
from brandledger.store import Store
from brandledger.vocab import load_vocab

__all__ = ["SERVICE_ID", "health", "build_api"]


def build_api(db_path):
    """在指定库文件上装配领域核心与 HTTP 接口。"""
    store = Store(db_path)
    return Api(Core(store))


def main():
    parser = argparse.ArgumentParser(description="赛事品牌权益")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="ledger.db", help="SQLite 库文件路径")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        vocab = load_vocab()
        with tempfile.TemporaryDirectory() as tmp:
            build_api(os.path.join(tmp, "check.db"))
        print(f"基础检查通过（{vocab.project}：权利类型 {len(vocab.rights_types)} 项，"
              f"合同版本 {len(vocab.contract_states)} 项，调整类型 {len(vocab.adjustment_kinds)} 项）")
    else:
        api = build_api(args.db)
        ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(api)).serve_forever()


if __name__ == "__main__":
    main()
