from apps.alarms.rules.relay_normalize import normalize_pf


class TestNormalizePf:
    def test_valid_pf_unchanged(self):
        assert normalize_pf(0.318) == 0.318
        assert normalize_pf(-0.95) == 0.95  # signo = dirección de flujo, no magnitud

    def test_percent_pf_scaled(self):
        # firmware que entrega pf entero en porcentaje (95) → fracción
        assert normalize_pf(95.0) == 0.95

    def test_absurd_pf_is_none(self):
        assert normalize_pf(5832.0) is None
        assert normalize_pf(None) is None
