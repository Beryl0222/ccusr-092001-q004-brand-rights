"""赛事品牌全年权益服务的基础入口。"""

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from rights import models
from rights.api import make_handler
from rights.services import RightsService
from rights.store import Store

SERVICE_ID = "event-brand-rights"


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def check():
    """校验 domain.json 的领域词汇与模型常量一致。"""
    vocab_path = Path(__file__).with_name("domain.json")
    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    problems = []
    if vocab.get("权利类型") != models.RIGHT_TYPES:
        problems.append("权利类型与模型不一致")
    if vocab.get("合同版本") != models.CONTRACT_STATUSES:
        problems.append("合同版本与模型不一致")
    adjustments = set(models.AMENDMENT_TYPES) | {models.UNIT_KIND_SALES, models.UNIT_KIND_REFUND}
    if set(vocab.get("调整类型", [])) != adjustments:
        problems.append("调整类型与模型不一致")
    if problems:
        raise SystemExit("基础检查失败：" + "；".join(problems))
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description="赛事品牌权益")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check()
        return
    service = RightsService(Store())
    handler = make_handler(service, health())
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
