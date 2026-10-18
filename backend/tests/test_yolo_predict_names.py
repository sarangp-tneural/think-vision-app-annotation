"""_yolo_predict_sync decodes class indexes with the stored class list, falling back to the weights' own names
when that list doesn't describe them (missing, or a different length)."""
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")
import server  # noqa: E402


class _Box:
    def __init__(self, cls):
        self.xyxy = [SimpleNamespace(tolist=lambda: [10, 10, 50, 50])]
        self.cls = [SimpleNamespace(item=lambda: cls)]
        self.conf = [SimpleNamespace(item=lambda: 0.9)]


def _predict(stored_classes, weight_names, cls_idx):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, "PNG")
    result = SimpleNamespace(boxes=[_Box(cls_idx)], names=weight_names)
    model = SimpleNamespace(predict=lambda *a, **k: [result])
    with patch.object(server, "_load_yolo", return_value=model):
        return server._yolo_predict_sync("w.pt", buf.getvalue(), stored_classes, 0.25)


def test_stored_class_list_is_used_when_it_matches_the_weights():
    boxes = _predict(["cat", "dog"], {0: "x", 1: "y"}, 1)
    assert [b["label"] for b in boxes] == ["dog"]


def test_weights_names_are_used_when_stored_list_has_another_length():
    boxes = _predict(["cat"], {0: "cat", 1: "dog", 2: "cow"}, 2)
    assert [b["label"] for b in boxes] == ["cow"]


def test_weights_names_are_used_when_stored_list_is_missing():
    boxes = _predict([], {0: "cat", 1: "dog"}, 1)
    assert [b["label"] for b in boxes] == ["dog"]
