from hermes_bridge.config import BridgeConfig, InstrumentConfig


def test_instrument_config_defaults_are_passthrough():
    inst = InstrumentConfig()
    assert inst.feed_timeframe == ""
    assert inst.decision_timeframe == "static"


def test_instrument_config_accepts_resampler_fields():
    inst = InstrumentConfig(feed_timeframe="1m", decision_timeframe="auto")
    assert inst.feed_timeframe == "1m"
    assert inst.decision_timeframe == "auto"


def test_bridge_config_default_instrument_passthrough():
    assert BridgeConfig().instrument.decision_timeframe == "static"
