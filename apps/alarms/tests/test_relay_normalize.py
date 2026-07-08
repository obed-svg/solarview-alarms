from apps.alarms.rules.relay_normalize import normalize_kw, normalize_pf


class TestNormalizePf:
    def test_valid_pf_unchanged(self):
        assert normalize_pf(0.318) == 0.318
        assert normalize_pf(-0.95) == 0.95  # signo = dirección de flujo, no magnitud

    def test_percent_pf_scaled(self):
        assert normalize_pf(95.0) == 0.95

    def test_absurd_pf_is_none(self):
        assert normalize_pf(5832.0) is None
        assert normalize_pf(None) is None


class TestNormalizeKw:
    def test_case_real_watts_in_1mw_plant(self):
        # visto en producción: kw=5832.96 con capacidad 1000 kW → era W
        kw, scale = normalize_kw(5832.96, capacity_kw=1000)

        assert kw == 5.83296
        assert scale == 0.001

    def test_plausible_kw_kept(self):
        # 850 como kW cabe en 1000 kW de capacidad; como W daría 0.85 kW,
        # también "plausible" → desempate por inversores generando ~840 kW
        kw, scale = normalize_kw(850.0, capacity_kw=1000, inverter_total_kw=840.0)

        assert kw == 850.0
        assert scale == 1.0

    def test_ambiguous_without_anchors_is_none(self):
        # sin capacidad ni inversores: 850 puede ser kW o W → no adivinar
        kw, scale = normalize_kw(850.0, capacity_kw=None, inverter_total_kw=None)

        assert kw is None

    def test_ambiguous_at_night_resolves_small_either_way(self):
        # de noche (inversores en 0) con ambas escalas plausibles → None
        kw, _ = normalize_kw(8.0, capacity_kw=1000, inverter_total_kw=0.0)

        assert kw is None

    def test_zero_is_zero(self):
        assert normalize_kw(0, capacity_kw=1000) == (0.0, 1.0)
