"""Allocate physical devices only after the case's participant plan exists."""

from artemis.runtime import device_pool
from artemis.runtime.multi_device.models import DeviceSpec


async def allocate_roles(plan, preferred_device=None):
    devices = [d for d in await device_pool.list_devices_async() if d.state == "device"]
    devices.sort(key=lambda d: (d.serial != preferred_device, d.is_busy, d.serial))
    if preferred_device and not any(d.serial == preferred_device for d in devices):
        raise ValueError(f"Requested device {preferred_device} is not online")
    if len(devices) < len(plan.roles):
        raise ValueError(
            f"Case requires {len(plan.roles)} distinct devices; only {len(devices)} are online"
        )
    # Busy devices may wait for leases; never collapse two roles onto one phone.
    return {role.id: DeviceSpec(serial=device.serial)
            for role, device in zip(plan.roles, devices)}
