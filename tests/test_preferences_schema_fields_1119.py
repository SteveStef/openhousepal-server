"""Every area field survives every save form (11/19: the update form had lost `townships`, so a chat or editor
change dropped the Showcase's townships without a word)."""
from app.schemas.collection_preferences import CollectionPreferencesBase, CollectionPreferencesUpdate


def test_update_form_keeps_every_area_field():
    body = {"cities": ["Exton, PA"], "townships": ["Uwchlan, PA"], "school_districts": ["Downingtown Area, PA"],
            "high_schools": ["nces:1"], "counties": ["Chester"]}
    for model in (CollectionPreferencesUpdate, CollectionPreferencesBase):
        got = model(**body).model_dump(exclude_unset=True)
        for k, v in body.items():
            assert got.get(k) == v, (model.__name__, k, got)


def test_update_form_fields_are_listed_once():
    import ast
    import inspect
    from app.schemas import collection_preferences as m
    tree = ast.parse(inspect.getsource(m))
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        names = [n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
        dup = {n for n in names if names.count(n) > 1}
        assert not dup, (cls.name, dup)
