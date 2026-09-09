"""M5 model contract: the masked loss, the class weights, and the checkpoint.

What is pinned here is what fails QUIETLY.

  - THE MASK. Most samples are unlabeled on most heads — that is the corpus's
    shape, not an error path. If the mask breaks, training does not crash: the
    sentinel -1 either becomes a real class index (teaching the gate that every
    unjudged cow is `not_cow`) or NaNs the loss for a whole batch.
  - THE CHECKPOINT'S SELF-DESCRIPTION. Head names and class ORDER travel with
    the weights. Read them out of band and a model trained before a poweruser
    added a class gets served with the new class list, shifting every prediction
    above the inserted index by one — over 46,486 rows, silently.

Runs on random weights (`pretrained=False`), so there is no weights download and
no network.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_model
    py -3.11 tests/test_instance_attrs_model.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.instance_attrs import model as M  # noqa: E402
from cownting.instance_attrs.train import NO_LABEL  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


HEADS = {"cow": ["not_cow", "cow"],
         "sun_exposure": ["shaded", "direct_sun", "not_visible"]}


def test_masked_loss_ignores_unlabeled_heads():
    import torch

    logits = {"cow": torch.randn(4, 2, requires_grad=True),
              "sun_exposure": torch.randn(4, 3, requires_grad=True)}
    # Two samples know they are cows and nothing else; two know their sun
    # exposure and carry no outline verdict. This IS the corpus's shape.
    targets = {"cow": torch.tensor([1, 0, NO_LABEL, NO_LABEL]),
               "sun_exposure": torch.tensor([NO_LABEL, NO_LABEL, 0, 2])}
    total, parts = M.masked_loss(logits, targets)
    check("both heads contribute despite every sample being half-unlabeled",
          set(parts) == {"cow", "sun_exposure"}, str(parts))
    check("the loss is finite", torch.isfinite(total).item(), str(total))
    total.backward()
    check("gradients reach both heads",
          logits["cow"].grad is not None and logits["sun_exposure"].grad is not None)


def test_a_head_with_no_labels_in_a_batch_is_skipped_not_nan():
    """cross_entropy over an empty selection returns nan, which would poison the
    whole batch's gradient. With 76 false positives in 628 samples, a batch
    where no sample carries a gate label is routine."""
    import torch

    logits = {"cow": torch.randn(3, 2, requires_grad=True),
              "sun_exposure": torch.randn(3, 3, requires_grad=True)}
    targets = {"cow": torch.tensor([NO_LABEL, NO_LABEL, NO_LABEL]),
               "sun_exposure": torch.tensor([0, 1, 2])}
    total, parts = M.masked_loss(logits, targets)
    check("the fully-unlabeled head contributes nothing", "cow" not in parts, str(parts))
    check("...and the loss stays finite rather than NaN",
          torch.isfinite(total).item(), str(total))

    # And the pathological case: nothing labeled at all still backprops legally.
    none = {"cow": torch.tensor([NO_LABEL] * 3), "sun_exposure": torch.tensor([NO_LABEL] * 3)}
    t2, p2 = M.masked_loss(logits, none)
    t2.backward()
    check("an all-unlabeled batch yields a real zero, not an exception",
          torch.isfinite(t2).item() and p2 == {}, f"{t2} {p2}")


def test_masked_loss_matches_plain_ce_on_the_labeled_subset():
    """The mask must SELECT, not reweight: masked loss over a batch that happens
    to be fully labeled has to equal ordinary cross-entropy exactly."""
    import torch
    import torch.nn.functional as F

    logit = torch.randn(6, 3)
    t = torch.tensor([0, 1, 2, 0, 1, 2])
    got, _ = M.masked_loss({"h": logit}, {"h": t})
    want = F.cross_entropy(logit, t)
    check("fully-labeled batch equals plain cross-entropy",
          torch.allclose(got, want), f"{float(got)} vs {float(want)}")

    # ...and padding it with unlabeled rows must not move the number.
    logit2 = torch.cat([logit, torch.randn(4, 3)])
    t2 = torch.cat([t, torch.tensor([NO_LABEL] * 4)])
    got2, _ = M.masked_loss({"h": logit2}, {"h": t2})
    check("unlabeled rows do not shift the loss",
          torch.allclose(got2, want), f"{float(got2)} vs {float(want)}")


def test_class_weights_lift_the_minority_class():
    import torch

    w = M.class_weights([443, 57])          # the real gate balance
    check("the rare class is weighted up", w[1] > w[0], str(w.tolist()))
    check("...roughly by the inverse frequency ratio",
          abs(float(w[1] / w[0]) - 443 / 57) < 0.01, str(w.tolist()))
    check("weights are normalised to mean 1 so heads stay comparable",
          abs(float(w.mean()) - 1.0) < 1e-6, str(float(w.mean())))
    check("a class with zero examples does not divide by zero",
          torch.isfinite(M.class_weights([10, 0])).all().item())


def test_checkpoint_round_trips_its_own_vocabulary():
    with tempfile.TemporaryDirectory() as d:
        import torch

        net = M.build_model(HEADS, pretrained=False)
        p = os.path.join(d, "m.pt")
        rev = M.save_checkpoint(
            p, model=net, heads=HEADS, img_size=224, metrics={"mean_macro_f1": 0.5},
            manifest_config={"crop_pad": 0.35, "crop_max_width": 768,
                             "mask_timestamp_banner": True})
        blob = M.load_checkpoint(p)
        check("head names and CLASS ORDER survive the round trip",
              blob["heads"] == HEADS, str(blob["heads"]))
        check("the crop geometry travels with the weights",
              blob["crop"]["crop_pad"] == 0.35
              and blob["crop"]["mask_timestamp_banner"] is True, str(blob["crop"]))
        check("the rev identifies the bytes on disk", blob["rev"] == rev,
              f"{blob['rev']} vs {rev}")

        x = torch.randn(2, 3, 224, 224)
        out = blob["model"](x)
        check("the rebuilt model has one output per head with the right width",
              {h: tuple(v.shape) for h, v in out.items()}
              == {"cow": (2, 2), "sun_exposure": (2, 3)},
              str({h: tuple(v.shape) for h, v in out.items()}))
        # Same weights, same input -> same logits. A silently re-initialised
        # head would still have the right SHAPE, so shape alone proves nothing.
        net.eval()
        with torch.no_grad():
            check("the loaded weights are the saved ones, not fresh ones",
                  torch.allclose(net(x)["cow"], out["cow"], atol=1e-6))


def test_a_checkpoint_from_another_layout_is_refused():
    """A silently mis-loaded model does not crash — it produces confident
    nonsense over every detection. Refusing is the only safe failure."""
    with tempfile.TemporaryDirectory() as d:
        import torch

        net = M.build_model(HEADS, pretrained=False)
        p = os.path.join(d, "m.pt")
        M.save_checkpoint(p, model=net, heads=HEADS, img_size=224, metrics={},
                          manifest_config={})
        blob = torch.load(p, weights_only=False)
        blob["version"] = M.CHECKPOINT_VERSION + 1
        torch.save(blob, p)
        try:
            M.load_checkpoint(p)
            check("a future checkpoint version is refused", False, "loaded anyway")
        except ValueError as e:
            check("a future checkpoint version is refused", "version" in str(e), str(e))


def main():
    print("=== test_instance_attrs_model ===")
    test_masked_loss_ignores_unlabeled_heads()
    test_a_head_with_no_labels_in_a_batch_is_skipped_not_nan()
    test_masked_loss_matches_plain_ce_on_the_labeled_subset()
    test_class_weights_lift_the_minority_class()
    test_checkpoint_round_trips_its_own_vocabulary()
    test_a_checkpoint_from_another_layout_is_refused()
    print("=================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
