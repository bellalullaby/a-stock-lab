"""Account interpretation only; never controls signals or order execution."""

CALIBRATION_META = {
    "role": "calibration",
    "note": "旧口径对照账户：买入时点含未来信息，收益率不代表可实现收益；研究口径见 data/execution_review.json",
    "since": "2026-09-29",
}


def account_notice(payload):
    meta = payload.get("account_meta", {})
    return meta.get("note", "") if meta.get("role") == "calibration" else ""
