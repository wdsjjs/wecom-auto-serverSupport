from cli_anything.wecom_gui.core import fixed_agent


def test_fixed_agent_detects_welcome_system_notice():
    message = {"role": "系统", "text": "你已添加了 三水儿，现在可以开始聊天了。"}

    assert fixed_agent.is_system_notice_message(message) is True


def test_fixed_agent_keeps_customer_mini_program_as_card_message():
    text = "UndoAge 营养工厂幸运大抽奖 WXMsg WeAppLogo 小程序"

    assert fixed_agent.is_card_or_link_message(text) is True


def test_fixed_agent_matches_supplement_first_prompt_as_service_text():
    text = fixed_agent.supplement_first_reply_with_profile()

    assert fixed_agent.is_fixed_service_text(text) is True
