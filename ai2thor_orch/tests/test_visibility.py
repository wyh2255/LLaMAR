"""Tests for AliasRegistry — alias generation, redact, raw id leak detection."""

from __future__ import annotations

import pytest

from ai2thor_orch.visibility import AliasRegistry


class TestRegister:
    def test_simple_alias(self):
        reg = AliasRegistry()
        alias = reg.register("Mug|-01.5|+00.9|+02.3")
        assert alias == "Mug_1"

    def test_same_raw_id_returns_same_alias(self):
        reg = AliasRegistry()
        a1 = reg.register("Mug|-01.5|+00.9|+02.3")
        a2 = reg.register("Mug|-01.5|+00.9|+02.3")
        assert a1 == a2 == "Mug_1"

    def test_different_raw_ids_get_different_aliases(self):
        reg = AliasRegistry()
        a1 = reg.register("Mug|-01.5|+00.9|+02.3")
        a2 = reg.register("Mug|+00.0|+00.0|+00.0")
        assert a1 == "Mug_1"
        assert a2 == "Mug_2"

    def test_different_types_have_independent_counters(self):
        reg = AliasRegistry()
        assert reg.register("Mug|-01.5|+00.9|+02.3") == "Mug_1"
        assert reg.register("Apple|+01.2|+00.5|+00.8") == "Apple_1"
        assert reg.register("Mug|+00.0|+00.0|+00.0") == "Mug_2"
        assert reg.register("Apple|+02.0|+00.0|+01.0") == "Apple_2"

    def test_alias_stability(self):
        """Aliases must be deterministic for the same registration order."""
        reg1 = AliasRegistry()
        reg2 = AliasRegistry()
        ids = ["Mug|-01.5|+00.9|+02.3", "Apple|+01.2|+00.5|+00.8"]
        for r in (reg1, reg2):
            for raw_id in ids:
                r.register(raw_id)
        assert reg1.alias("Mug|-01.5|+00.9|+02.3") == reg2.alias("Mug|-01.5|+00.9|+02.3")
        assert reg1.alias("Apple|+01.2|+00.5|+00.8") == reg2.alias("Apple|+01.2|+00.5|+00.8")


class TestAlias:
    def test_known_raw_id(self):
        reg = AliasRegistry()
        reg.register("Mug|-01.5|+00.9|+02.3")
        assert reg.alias("Mug|-01.5|+00.9|+02.3") == "Mug_1"

    def test_unknown_raw_id(self):
        reg = AliasRegistry()
        assert reg.alias("Unknown|+00|+00|+00") is None


class TestRaw:
    def test_reverse_lookup(self):
        reg = AliasRegistry()
        raw = "Mug|-01.5|+00.9|+02.3"
        alias = reg.register(raw)
        assert reg.raw(alias) == raw

    def test_unknown_alias(self):
        reg = AliasRegistry()
        assert reg.raw("Mug_999") is None


class TestRedact:
    def test_replaces_raw_ids_in_text(self):
        reg = AliasRegistry()
        text = "You see Mug|-01.5|+00.9|+02.3 on the CounterTop|+00.0|+00.0|+00.0."
        result = reg.redact(text)
        assert "Mug_1" in result
        assert "CounterTop_1" in result
        assert "|-01.5|+00.9|+02.3" not in result
        assert "|+00.0|+00.0|+00.0" not in result

    def test_redact_does_not_affect_clean_text(self):
        reg = AliasRegistry()
        text = "Everything looks normal here."
        result = reg.redact(text)
        assert result == text

    def test_redact_auto_registers(self):
        """Unregistered raw ids in redact() should be auto-registered."""
        reg = AliasRegistry()
        result = reg.redact("Found Mug|+01.0|+02.0|+03.0")
        assert "Mug_1" in result
        assert reg.alias("Mug|+01.0|+02.0|+03.0") == "Mug_1"


class TestIsRawIdLeaked:
    def test_detects_raw_id_pattern(self):
        reg = AliasRegistry()
        assert reg.is_raw_id_leaked("Mug|-01.5|+00.9|+02.3") is True
        assert reg.is_raw_id_leaked("CounterTop|+00.0|+00.0|+00.0") is True

    def test_clean_text_returns_false(self):
        reg = AliasRegistry()
        assert reg.is_raw_id_leaked("Mug_1 is on CounterTop_1") is False
        assert reg.is_raw_id_leaked("") is False

    def test_audit_scenario(self):
        """Simulate the G4.7 audit: worker snapshot should not contain raw ids."""
        reg = AliasRegistry()
        # This is what a raw metadata objectId looks like
        dirty_text = (
            "You are at position (0.0, 0.0, 0.0). "
            "You see Mug|-01.5|+00.9|+02.3 and Apple|+01.2|+00.5|+00.8."
        )
        assert reg.is_raw_id_leaked(dirty_text) is True

        cleaned = reg.redact(dirty_text)
        assert reg.is_raw_id_leaked(cleaned) is False
        assert "Mug_1" in cleaned
        assert "Apple_1" in cleaned


class TestSize:
    def test_size_grows_with_registrations(self):
        reg = AliasRegistry()
        assert reg.size == 0
        reg.register("Mug|-01.5|+00.9|+02.3")
        assert reg.size == 1
        reg.register("Apple|+01.2|+00.5|+00.8")
        assert reg.size == 2
