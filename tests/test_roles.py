from crmlumi.roles import ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER, role_names


def test_role_names_lists_all_six_roles():
    names = role_names()
    assert len(names) == 6
    assert ROLE_NEW_LEADS in names
    assert ROLE_PHOTOGRAPHER in names


def test_role_names_order_is_stable():
    # Индекс роли в этом списке используется в callback_data кнопок —
    # порядок должен быть детерминирован.
    assert role_names() == role_names()
