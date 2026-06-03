"""Fixed message configuration helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_FIXED_MESSAGES = {
    "welcome": {
        "wecom_fixed_welcome": "{WELCOME_MESSAGE}",
        "supplement_web_welcome_template": (
            "您好，{用户名}\n"
            "我是您专属的——Luna 营养工厂健康顾问\n"
            "✓已为超 40 万人提供营养咨询服务\n\n"
            "领产品说明书 https://docs.qq.com/s/tHMpjD9S811JnjY369QC2G\n\n"
            "留下您的性别、年龄和需求，我为您【搭配补剂】。比如失眠、肥胖、脱发、三高…等等～\n\n"
            "👇【今日限时福利】进群领 50 元券包🎁\n"
            "docs.qq.com\n"
            "docs.qq.com"
        ),
    },
    "supplement": {
        "profile_prompt": "您好~可以简单介绍下您的基本信息（年龄、性别、身高、体重等），方便了解您的身体状况哦～",
        "need_choices_text": (
            "请问您想改善哪方面呢，请选择您最关心的3-5个需求，可以直接回复【数字】：\n"
            "1.抗衰/身体机能下降\n"
            "2.睡眠质量差\n"
            "3.身体代谢差/免疫力\n"
            "4.白发、脱发、头皮活力\n"
            "5.减脂减重/改善体型\n"
            "6.皮肤松弛暗沉/长痘痘\n"
            "7.晨起疲惫，精神不振\n"
            "8.每天用脑超八小时，注意力难集中\n"
            "9.抑郁焦虑，情绪差\n"
            "10.肝脏排毒功能差，熬夜伤肝\n"
            "11.办公室久坐不动人群\n"
            "12.用眼过度，眼疲劳\n"
            "13.女性保养\n"
            "14.肠胃不好，便秘或菌群失调\n"
            "15.男性性功能问题\n"
            "16备孕支持\n"
            "17.运动健身人群\n"
            "18.需促进骨骼健康，强健骨质\n"
            "19.经常抽烟，烟瘾重\n"
            "20.儿童成长，助力身体发育"
        ),
        "first_reply_with_profile_template": "{profile_prompt}\n{need_choices_text}",
        "first_reply_choices_only_template": "您好~\n{need_choices_text}",
        "selection_ack": "收到，我先根据您选择的需求和基础信息做匹配，请稍等一下～",
        "recommendation_intro_up_to_three": "结合您的需求，为您推荐这几款产品组合。接下来，我详细为您介绍下：",
        "recommendation_intro_over_three": "结合您的需求，优先为您推荐这几款产品组合。接下来，我详细为您介绍下：",
        "recommendation_followup_over_three": "如果您服用后感觉效果良好，后续可以搭配以下产品：",
    },
}


def config_path() -> Path:
    configured = os.environ.get("WECOM_GUI_FIXED_MESSAGES_FILE") or os.environ.get("FIXED_MESSAGES_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[4] / "config" / "fixed_messages.json"


def load_fixed_messages() -> dict:
    data = json.loads(json.dumps(DEFAULT_FIXED_MESSAGES, ensure_ascii=False))
    path = config_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(loaded, dict):
        return data
    for section, values in loaded.items():
        if not isinstance(values, dict):
            continue
        target = data.setdefault(str(section), {})
        for key, value in values.items():
            if isinstance(value, str):
                target[str(key)] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                target[str(key)] = "\n".join(value)
    return data


def fixed_message(section: str, key: str) -> str:
    values = load_fixed_messages().get(section, {})
    if not isinstance(values, dict):
        return ""
    return str(values.get(key) or "")


def supplement_profile_prompt() -> str:
    return fixed_message("supplement", "profile_prompt")


def supplement_need_choices_text() -> str:
    return fixed_message("supplement", "need_choices_text")


def supplement_first_reply_with_profile() -> str:
    values = load_fixed_messages()["supplement"]
    return str(values["first_reply_with_profile_template"]).format(
        profile_prompt=str(values["profile_prompt"]),
        need_choices_text=str(values["need_choices_text"]),
    )


def supplement_first_reply_choices_only() -> str:
    values = load_fixed_messages()["supplement"]
    return str(values["first_reply_choices_only_template"]).format(
        profile_prompt=str(values["profile_prompt"]),
        need_choices_text=str(values["need_choices_text"]),
    )
