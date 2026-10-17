"""Describe the actual device binding to single-device model callers."""


def single_device_instructions(ctx) -> str:
    serial = getattr(getattr(ctx, "device", None), "device_id", None) or "the bound device"
    return (
        "\n# Device execution scope\n"
        f"This is a SINGLE-DEVICE run. Every observation and device action is bound to {serial}. "
        "Other connected devices are not available to this run. Naming Device A/B in a plan "
        "does not create a role binding or change the action target. "
        "If the task requires another physical device, report the missing multi-device binding "
        "and stop device operations; direct the user to Pro mode for automatic role planning. "
        "Do not search Android Settings for a device switcher, switch Android users/Guest, "
        "or use an app/account/profile on this phone as a substitute for another physical device. "
        "An explicit task to manage users or accounts on this same device is still allowed.\n"
    )
