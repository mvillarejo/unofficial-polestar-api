# Unofficial Polestar for Home Assistant

Unofficial Home Assistant custom integration for Polestar vehicles. Communicates with Polestar's gRPC backend directly via [`unofficial-polestar-api`](https://pypi.org/project/unofficial-polestar-api/).

## Install

Requires [HACS](https://hacs.xyz/) installed on your Home Assistant instance.

1. In HA, go to **HACS → ⋮ (top right) → Custom repositories**
2. Paste `kildahldev/unofficial-polestar-api` and select **Integration**
3. Click **Add**, then find **Unofficial Polestar** in HACS and click **Download**
4. Restart Home Assistant

Then add the integration via **Settings → Devices & Services → Add Integration → Polestar**. Enter your Polestar ID email and password — the integration discovers the cars on your account and lets you **pick your vehicle from a list** and set the polling interval. Each config entry sets up one vehicle — to add more than one car, add the integration again and pick the other vehicle.

- **Secondary / guest accounts** (no vehicles listed): choose **"My vehicle is not listed"** and enter the VIN (Vehicle Identification Number) manually.
- **Demo mode:** tick **Demo mode** on the first step and enter any VIN to get a fake vehicle with static data (no API connection needed).

### Setup parameters

| Step | Field | Required | Description |
|------|-------|----------|-------------|
| Credentials | **Email** | Yes | The email address of your Polestar account. |
| Credentials | **Password** | Yes | The password of your Polestar account. |
| Credentials | **Demo mode** | No (default off) | Create a fake vehicle with sample data instead of connecting to Polestar. |
| Select your vehicle | **Vehicle** | Yes | The car to add, picked from the ones found on the account. Pick the last option if your car is missing. |
| Select your vehicle | **Polling interval** | No (default 600) | Seconds between polls of the data that is not pushed live, 60–86400. |
| Enter VIN manually | **VIN** | Yes | The 17-character VIN of the vehicle you have access to. Shown when the account lists no vehicles, or when you pick "My vehicle is not listed". |
| Enter VIN manually | **Polling interval** | No (default 600) | Seconds between polls of the data that is not pushed live, 60–86400. |
| Demo vehicle VIN | **VIN** | Yes | Any 17-character identifier for the fake demo vehicle. Shown only in demo mode. |

The vehicle picker and the manual VIN step are alternatives — you see one or the other, never both. Re-authentication later asks for the **Password** only. The polling interval is the only setting you can change after setup, under [Update interval](#update-interval).

## Remove

1. Go to **Settings → Devices & Services → Polestar**
2. Click **⋮** on the vehicle entry you want to remove and choose **Delete**

Deleting the entry removes its device, entities, and the stored session tokens. To remove the integration itself, delete every entry first, then uninstall **Unofficial Polestar** from HACS and restart Home Assistant.

## Entities

Each vehicle gets ~106 entities when all features are available:
Features vary between models, and are yet to be fully documented

| Platform | Count | Examples |
|----------|-------|---------|
| Sensor | 48 | Battery level, charging status, availability reason, climate status, health warnings, tyre pressures, charge locations |
| Binary sensor | 25 | Charging, plugged in, locked, doors, windows, hood, tailgate, tank lid, available |
| Device tracker | 2 | GPS location, parked location |
| Lock | 1 | Central lock |
| Switch | 4 | Climate, pre-cleaning, charging, charge timer |
| Number | 4 | Target SOC, charging amp limit, climate target temperature, timer target temperature |
| Button | 7 | Flash lights, honk, honk and flash, refresh, open/close windows, unlock trunk |
| Select | 11 | Target SOC mode, climate start seat/steering heating, climate timer default seat/steering heating and battery preconditioning |
| Time | 2 | Charge timer start and stop |
| Calendar | 3 | Parking climate timer slots |
| Update | 1 | OTA software update |

Entity IDs are generated from the vehicle model and registration. For example, a Polestar 4 registered `ES59205` will create entities like `sensor.polestar_4_es59205_battery_level`, `lock.polestar_4_es59205_lock`, and `button.polestar_4_es59205_refresh`.

## Update interval

The integration polls the Polestar cloud for updates every **10 minutes** by default. To change this:

1. Go to **Settings → Devices & Services → Polestar**
2. Click **Configure** on your vehicle entry
3. Set the **Polling interval** (in seconds, minimum 60, maximum 86400)

Lower values give faster updates but may increase server load and 12v battery drain. In addition to polling, the integration keeps long-lived streams open for real-time updates (battery, location, door status, etc.), so polling mainly covers data that isn't streamed.

## Services

The integration also registers custom services for the parts of the Polestar API that do not fit a single HA entity cleanly:

- `polestar.start_climate`
- `polestar.set_charge_timer`
- `polestar.clear_charge_timer`
- `polestar.create_charge_location`
- `polestar.update_charge_location`
- `polestar.delete_charge_location`
- `polestar.schedule_ota`
- `polestar.cancel_ota`
- `polestar.delete_climate_timer`
