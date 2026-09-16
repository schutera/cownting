"""The training recipe's moving parts: folds, backbones, tempered weights, TTA.

Each of these can fail without an error. A fold assignment that lets a block
straddle two folds leaks near-duplicate crops into validation and inflates
every CV number; a checkpoint that forgets its backbone loads a resnet
state_dict into a mobilenet and raises — fine — but one that forgets it and
happens to be the DEFAULT backbone loads silently; a class-weight power that
is not applied leaves the rare class at 15x with nobody noticing; a TTA that
averages probabilities instead of logits changes rankings quietly.

Random weights throughout (`pretrained=False`); no downloads.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_recipe
    py -3.11 tests/test_instance_attrs_recipe.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.config import InstanceAttrsCfg  # noqa: E402
from cownting.instance_attrs import cv as CV  # noqa: E402
from cownting.instance_attrs import model as M  # noqa: E402
from cownting.instance_attrs import train as T  # noqa: E402

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


HEADS = {"cow": ["not_cow", "cow"], "sun_exposure": ["a", "b", "c"]}


# ------------------------------------------------------------------------ folds

def test_folds_are_whole_blocks_balanced_by_samples():
    blocks = {f"b{i}": (i % 7) + 1 for i in range(60)}      # sizes 1..7
    assign = CV.fold_blocks(blocks, k=5, seed=1)
    check("every block gets exactly one fold", set(assign) == set(blocks))
    check("every fold index is in range", set(assign.values()) <= set(range(5)))
    loads = [sum(n for b, n in blocks.items() if assign[b] == f) for f in range(5)]
    total = sum(blocks.values())
    check("folds are balanced by SAMPLE count, not block count",
          max(loads) - min(loads) <= 7, f"loads={loads} total={total}")
    check("...and no fold is empty", min(loads) > 0, str(loads))


def test_folds_are_deterministic_and_seed_sensitive():
    blocks = {f"b{i}": 5 for i in range(40)}                 # all equal: only the seed decides
    a = CV.fold_blocks(blocks, k=4, seed=1)
    b = CV.fold_blocks(blocks, k=4, seed=1)
    c = CV.fold_blocks(blocks, k=4, seed=2)
    check("same seed, same folds", a == b)
    check("a different seed moves blocks between folds", a != c)


def test_an_oversized_block_cannot_be_split():
    """The property everything rests on: a block is in ONE fold. A 1000-sample
    block among 2-sample ones goes somewhere whole; it is never sliced to
    balance the loads, however unbalanced that leaves them."""
    blocks = {"huge": 1000, **{f"s{i}": 2 for i in range(20)}}
    assign = CV.fold_blocks(blocks, k=5, seed=3)
    check("the huge block is in exactly one fold", isinstance(assign["huge"], int))
    others = [f for b, f in assign.items() if b != "huge"]
    check("...and the small ones avoid its fold where they can",
          others.count(assign["huge"]) <= 1, str(others.count(assign["huge"])))


# -------------------------------------------------------------------- backbones

def test_checkpoint_carries_its_backbone():
    """Loading a resnet state_dict into a mobilenet raises loudly — fine. The
    dangerous case is the OTHER direction: a checkpoint that does not say what
    it is gets the default trunk, and if that happens to fit, it loads and
    serves nonsense. So the trunk name travels with the weights."""
    import torch

    with tempfile.TemporaryDirectory() as d:
        for bb in M.BACKBONES:
            net = M.build_model(HEADS, backbone=bb, pretrained=False)
            p = os.path.join(d, f"{bb}.pt")
            M.save_checkpoint(p, model=net, heads=HEADS, img_size=64, metrics={},
                              manifest_config={})
            blob = M.load_checkpoint(p)
            check(f"{bb}: the checkpoint names its trunk", blob["backbone"] == bb,
                  blob.get("backbone"))
            x = torch.randn(1, 3, 64, 64)
            net.eval()
            with torch.no_grad():
                same = torch.allclose(net(x)["cow"], blob["model"](x)["cow"], atol=1e-5)
            check(f"{bb}: reloaded weights reproduce the same logits", same)
        # A checkpoint from before the field existed IS a mobilenet_v3_small.
        old = torch.load(os.path.join(d, "mobilenet_v3_small.pt"), weights_only=False)
        del old["backbone"]
        torch.save(old, os.path.join(d, "legacy.pt"))
        blob = M.load_checkpoint(os.path.join(d, "legacy.pt"))
        check("a checkpoint without the field defaults to the original trunk",
              blob["model"].backbone == M.DEFAULT_BACKBONE)


def test_unknown_backbone_is_refused():
    try:
        M.build_model(HEADS, backbone="vit_h_14", pretrained=False)
        check("an unknown backbone raises", False, "built anyway")
    except ValueError as e:
        check("an unknown backbone raises", "vit_h_14" in str(e), str(e))


# ---------------------------------------------------------------- class weights

def test_weight_power_tempers_the_rare_class():
    """The real behaviour balance: feeding 601, head_probing 40."""
    import torch

    full = M.class_weights([601, 403, 365, 40, 160], power=1.0)
    half = M.class_weights([601, 403, 365, 40, 160], power=0.5)
    ratio_full = float(full[3] / full[0])
    ratio_half = float(half[3] / half[0])
    check("at power 1 the rare class is ~15x the common one",
          14 < ratio_full < 16, f"{ratio_full:.1f}x")
    check("at power 0.5 that is tempered to ~4x", 3.5 < ratio_half < 4.2, f"{ratio_half:.1f}x")
    check("...while still favouring the rare class", ratio_half > 1)
    check("both stay normalised to mean 1",
          abs(float(full.mean()) - 1) < 1e-6 and abs(float(half.mean()) - 1) < 1e-6)
    flat = M.class_weights([601, 40], power=0.0)
    check("power 0 is no weighting at all", torch.allclose(flat, torch.ones(2)))


# ------------------------------------------------------------------------- tta

def test_tta_flips_the_width_axis_and_averages_logits():
    """Proven on a stub whose output is ANTISYMMETRIC under a horizontal flip:
    left-half sum minus right-half sum. Then the flipped view is exactly the
    negation, the logit mean is exactly zero, and a symmetric input gives zero
    both ways. A real random-init net cannot serve here — its output barely
    depends on the input, so "TTA differs from the single view" is not a claim
    it can support either way."""
    import torch
    import torch.nn as nn

    class LeftMinusRight(nn.Module):
        def forward(self, x):
            w = x.shape[3]
            v = x[:, :, :, : w // 2].sum(dim=(1, 2, 3)) - x[:, :, :, w // 2:].sum(dim=(1, 2, 3))
            return {"cow": v.unsqueeze(1)}

    net = LeftMinusRight().eval()
    x = torch.randn(3, 3, 8, 8)
    with torch.no_grad():
        plain = T._forward(net, x, tta_hflip=False)["cow"]
        tta = T._forward(net, x, tta_hflip=True)["cow"]
        manual = (net(x)["cow"] + net(torch.flip(x, dims=[3]))["cow"]) / 2
    check("the single view is non-zero on a random crop",
          bool((plain.abs() > 1e-4).all()), str(plain.flatten().tolist()))
    check("TTA is the mean of the two views' LOGITS",
          torch.allclose(tta, manual, atol=1e-6))
    check("...which for an antisymmetric function is exactly zero — the flip IS the width axis",
          bool((tta.abs() < 1e-5).all()), str(tta.flatten().tolist()))
    # A flip on any OTHER axis would not cancel this stub, so the zero above is
    # the proof. Belt and braces: a symmetric input is unchanged by TTA.
    half = torch.randn(1, 3, 8, 4)
    sym = torch.cat([half, torch.flip(half, dims=[3])], dim=3)
    with torch.no_grad():
        a = T._forward(net, sym, tta_hflip=False)["cow"]
        b = T._forward(net, sym, tta_hflip=True)["cow"]
    check("a symmetric input is unchanged by TTA", torch.allclose(a, b, atol=1e-5))


# --------------------------------------------------------------- size reweight

def test_size_reweight_draws_the_production_mix():
    """The labelled corpus is ~18% 25–60px; production is ~48%. The sampler must
    turn the former into the latter in expectation — and be OFF by default, or
    every existing number silently changes."""
    import torch
    from torch.utils.data import WeightedRandomSampler

    check("size_reweight is off by default", InstanceAttrsCfg().size_reweight is False)
    target = InstanceAttrsCfg().size_target_shares
    check("the default target is production's measured mix and sums to 1",
          abs(sum(target.values()) - 1.0) < 1e-9 and target["25-60"] == 0.48, str(target))

    # A corpus with the labelled data's skew: 1% / 17% / 37% / 45%.
    def item(side):
        return {"bbox": [0, 0, side, side], "labels": {"cow": "cow"}}
    items = [item(15)] * 10 + [item(40)] * 170 + [item(75)] * 370 + [item(150)] * 450
    w, mult = T.size_sample_weights(items, target)
    check("a 25–60px item is upweighted ~2.8x its corpus share",
          2.6 < mult["25-60"] < 3.0, str(mult))
    check("a 90+ item is downweighted to ~0.5x", 0.45 < mult["90+"] < 0.55, str(mult))
    check("one weight per item, in item order",
          len(w) == len(items) and w[0] == mult["<25"] and w[-1] == mult["90+"])

    gen = torch.Generator().manual_seed(0)
    s = WeightedRandomSampler(torch.tensor(w, dtype=torch.double), num_samples=40000,
                              replacement=True, generator=gen)
    from cownting.instance_attrs.strata import band_of, long_side
    drawn = [band_of(long_side(items[i])) for i in s]
    for b, share in target.items():
        got = drawn.count(b) / len(drawn)
        check(f"drawn share of {b} matches the target ({share:.2f})",
              abs(got - share) < 0.02, f"got {got:.3f}")

    # Effective class counts follow the sampling, not the raw list.
    items2 = [{"bbox": [0, 0, 40, 40], "labels": {"cow": "not_cow"}},
              {"bbox": [0, 0, 150, 150], "labels": {"cow": "cow"}}]
    w2, _ = T.size_sample_weights(items2, {"25-60": 0.9, "90+": 0.1})
    raw = T._class_counts(items2, {"cow": ["not_cow", "cow"]})
    eff = T._class_counts(items2, {"cow": ["not_cow", "cow"]}, w2)
    check("raw counts are one each", raw["cow"] == [1.0, 1.0], str(raw))
    check("effective counts weight the small crop up and the big one down",
          eff["cow"][0] > eff["cow"][1], str(eff))


# --------------------------------------------------------------- augmentation

def test_augmentation_knobs_are_on_by_default_and_zero_turns_them_off():
    from torchvision import transforms as TV

    # The CV-chosen defaults: augmentation ON. Pinned, because it was the one
    # recipe change that measurably helped and a refactor that quietly zeroed
    # it would cost 0.01 pooled and 0.04 on the 25–60px gate without failing.
    d = InstanceAttrsCfg()
    check("defaults: rotation, erasing and blur are on",
          d.aug_rotate_deg > 0 and d.aug_erase_p > 0 and d.aug_blur_p > 0,
          f"{d.aug_rotate_deg} {d.aug_erase_p} {d.aug_blur_p}")
    off = T._transforms(InstanceAttrsCfg(img_size=64, aug_rotate_deg=0, aug_erase_p=0,
                                         aug_blur_p=0), train=True)
    names = [type(t).__name__ for t in off.transforms]
    check("zeroed: no rotation, no erasing, no blur",
          not any(n in names for n in ("RandomRotation", "RandomErasing", "RandomApply")),
          str(names))
    check("...only the label-safe geometry remains",
          "RandomResizedCrop" in names and "RandomHorizontalFlip" in names, str(names))

    on = T._transforms(InstanceAttrsCfg(img_size=64, aug_rotate_deg=10, aug_erase_p=0.3,
                                        aug_blur_p=0.2), train=True)
    names = [type(t).__name__ for t in on.transforms]
    check("set: rotation, erasing and blur are present",
          all(n in names for n in ("RandomRotation", "RandomErasing", "RandomApply")),
          str(names))
    check("erasing runs AFTER normalisation, on the tensor",
          names.index("RandomErasing") > names.index("Normalize"), str(names))
    check("no photometric jitter is ever added",
          not any(isinstance(t, (TV.ColorJitter, TV.RandomGrayscale)) for t in on.transforms))
    val = T._transforms(InstanceAttrsCfg(img_size=64, aug_rotate_deg=10, aug_erase_p=0.3,
                                         aug_blur_p=0.2), train=False)
    vnames = [type(t).__name__ for t in val.transforms]
    check("the val transform ignores every augmentation knob",
          vnames == ["Resize", "ToTensor", "Normalize"], str(vnames))


def main():
    print("=== test_instance_attrs_recipe ===")
    test_folds_are_whole_blocks_balanced_by_samples()
    test_folds_are_deterministic_and_seed_sensitive()
    test_an_oversized_block_cannot_be_split()
    test_checkpoint_carries_its_backbone()
    test_unknown_backbone_is_refused()
    test_weight_power_tempers_the_rare_class()
    test_tta_flips_the_width_axis_and_averages_logits()
    test_size_reweight_draws_the_production_mix()
    test_augmentation_knobs_are_on_by_default_and_zero_turns_them_off()
    print("==================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
