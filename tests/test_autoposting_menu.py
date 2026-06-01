from keyboards.menus import get_autoposting_menu


def test_autoposting_menu_has_quick_report_button():
    menu = get_autoposting_menu()
    callback_values = [
        button.callback_data
        for row in menu.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "autopost_quick_report" in callback_values
