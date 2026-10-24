"""Which devices a video call records.

The laptop microphone saved at setup is not the headset microphone, and a
tap of the system default output misses a call that is only playing in the
headphones.
"""

from meeting_archive.audio import (
    CoreDevice,
    choose_call_route,
    parse_coreaudio_devices,
    shell_quote,
)


def _device(name, transport, **kwargs):
    return CoreDevice(name=name, transport=transport, **kwargs)


def test_no_headset_uses_the_current_mic_and_speakers():
    devices = [
        _device("MacBook Neo Microphone", "builtin", input_channels=1, default_input=True),
        _device("MacBook Neo Speakers", "builtin", output_channels=2, default_output=True),
    ]
    route = choose_call_route(devices, "MacBook Neo Microphone")
    assert route.microphone == "MacBook Neo Microphone"
    assert route.playback == ("MacBook Neo Speakers",)


def test_bluetooth_headphones_keep_the_laptop_microphone():
    """Opening the AirPods microphone drops them to phone quality and the
    audio already playing through them is then missing from the recording."""
    devices = [
        _device("MacBook Neo Microphone", "builtin", input_channels=1),
        _device("MacBook Neo Speakers", "builtin", output_channels=2),
        _device("Jacob's AirPods", "bluetooth", output_channels=2, default_output=True),
        _device("Jacob's AirPods", "bluetooth", input_channels=1, default_input=True),
    ]
    route = choose_call_route(devices, "MacBook Neo Microphone")
    assert route.microphone == "MacBook Neo Microphone"
    assert route.playback == ("Jacob's AirPods",)


def test_headphone_jack_keeps_the_laptop_microphone():
    """A 3.5mm jack has no microphone of its own. The output source is the
    only thing that changes on the built-in device."""
    devices = [
        _device("MacBook Neo Microphone", "builtin", input_channels=1, default_input=True),
        _device(
            "MacBook Neo Speakers",
            "builtin",
            output_channels=2,
            default_output=True,
            output_source="Headphones",
        ),
    ]
    route = choose_call_route(devices, "MacBook Neo Microphone")
    assert route.microphone == "MacBook Neo Microphone"
    assert route.playback == ("MacBook Neo Speakers",)


def test_a_usb_dock_is_not_treated_as_headphones():
    devices = [
        _device("MacBook Neo Microphone", "builtin", input_channels=1, default_input=True),
        _device("MacBook Neo Speakers", "builtin", output_channels=2, default_output=True),
        _device("Elgato Dock", "usb", output_channels=2),
    ]
    route = choose_call_route(devices, "MacBook Neo Microphone")
    assert route.playback == ("MacBook Neo Speakers",)
    assert route.microphone == "MacBook Neo Microphone"


def test_every_connected_headset_output_is_recorded():
    """The meeting app can play into either pair. Tapping only the system
    default misses the other one."""
    devices = [
        _device("MacBook Pro Microphone", "builtin", input_channels=1, default_input=True),
        _device("MacBook Pro Speakers", "builtin", output_channels=2, default_output=True),
        _device("AirPods Pro", "bluetooth", output_channels=2),
        _device("Office Headset", "usb", output_channels=2, input_channels=1),
    ]
    route = choose_call_route(devices, "MacBook Pro Microphone")
    assert route.playback == ("AirPods Pro", "Office Headset")
    # The AirPods microphone would spoil their output. The wired headset
    # microphone does not.
    assert route.microphone == "Office Headset"


def test_profiler_json_marks_defaults_and_the_headphone_jack():
    payload = """
    {"SPAudioDataType": [{"_items": [
      {"_name": "MacBook Neo Microphone",
       "coreaudio_default_audio_input_device": "spaudio_yes",
       "coreaudio_device_input": 1,
       "coreaudio_device_transport": "coreaudio_device_type_builtin"},
      {"_name": "MacBook Neo Speakers",
       "coreaudio_default_audio_output_device": "spaudio_yes",
       "coreaudio_device_output": 2,
       "coreaudio_device_transport": "coreaudio_device_type_builtin",
       "coreaudio_output_source": "Headphones"}
    ]}]}
    """
    devices = parse_coreaudio_devices(payload)
    route = choose_call_route(devices, "")
    assert route.microphone == "MacBook Neo Microphone"
    assert route.playback == ("MacBook Neo Speakers",)


def test_shell_quote_survives_an_apostrophe_in_the_device_name():
    assert shell_quote("Jacob's AirPods") == "'Jacob'\\''s AirPods'"
