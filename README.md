

# unofficial-polestar-api

> **Fork notice:** this is a fork of [kildahldev/unofficial-polestar-api](https://github.com/kildahldev/unofficial-polestar-api),
> maintained by [Manuel Villarejo](https://github.com/mvillarejo). Fixes and features developed here that are
> generally useful are intended to be upstreamed via pull request when ready; in the meantime this fork
> tracks its own releases and the HA integration installs the library directly from here (see `manifest.json`)
> rather than from PyPI, since only the upstream project can publish under the shared package name.

Unofficial async Python client and Home Assistant integration for Polestar gRPC APIs.

This project aims to bring you as much control as possible over your car. It uses the same APIs as the official mobile app and exposes most functionality.

> **Note on 12v battery impact:** This library communicates with Polestar's cloud servers, not the car directly. It polls the server every 10 minutes (default, configurable in HA) but it also keeps long lived streams open to the cloud to listen to changes (Battery, Location, Door status etc). It is unclear how much, and if this affects the 12v battery.  
If you have the opportunity, please monitor your battery voltage and report back.
## Supported Cars

This library implements the **C3** (Volvo Cars Cloud Connectivity) backend.
If you use this library (or the HA integration) please report back what works and what doesn't, for your model.
Contributions and testing from owners of other models are welcome and encouraged  
Not all features are available on all models. Look at the features list for some comments on different models.

## Usage

### Home Assistant

Requires [HACS](https://hacs.xyz/) installed on your Home Assistant instance.

1. In HA, go to **HACS → ⋮ (top right) → Custom repositories**
2. Paste `kildahldev/unofficial-polestar-api` and select **Integration**
3. Click **Add**, then find **Unofficial Polestar** in HACS and click **Download**
4. Restart Home Assistant

Then add the integration via **Settings → Devices & Services → Add Integration → Polestar**. Enter your Polestar ID email and password — the integration discovers the cars on your account and lets you **pick your vehicle from a list** and set the polling interval. Each config entry sets up one vehicle — to add more than one car, add the integration again and pick the other vehicle.

- **Secondary / guest accounts** (no vehicles listed): choose **"My vehicle is not listed"** and enter the VIN (Vehicle Identification Number) manually.
- **Demo mode:** tick **Demo mode** on the first step and enter any VIN to get a fake vehicle with static data (no API connection needed).

See the [HA integration README](ha_integration_README.md) for setup, entities, services, and more dashboard card examples.

#### Example dashboard

A complete single-vehicle dashboard card, covering quick controls (lock, climate,
charging, location), battery/range gauges, a grouped charging panel with the
charging-time-to-target sensors, a collapsible seat-heat section, doors/windows,
a map, and a collapsible service/health panel.

Requires [mushroom-cards](https://github.com/piitaya/lovelace-mushroom) via HACS.
Find-and-replace `polestar_VIN` with your entity prefix (e.g. from
`sensor.polestar_4_es59205_battery_level`, that's `polestar_4_es59205`). The
`input_boolean.polestar_clima_expandido` and `input_boolean.polestar_salud_expandida`
helpers used for the collapsible sections must be created first (Settings →
Devices & Services → Helpers → Toggle). Depending on your setup, some entity IDs
may get an area-name prefix HA adds automatically (e.g.
`sensor.garage_polestar_VIN_charging_time_domestic_power`) — check your actual
entity IDs if a card shows as unavailable.

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-title-card
    title: Polestar 4
    subtitle: '{{ states(''sensor.polestar_VIN_battery_level'')|int(0) }}% · {{
      states(''sensor.polestar_VIN_range'') }} km · {{ states(''sensor.polestar_VIN_outside_temperature'')
      }}°C exterior · Clima {{ ''encendido'' if is_state(''switch.polestar_VIN_climate'',''on'')
      else ''apagado'' }} ({{ states(''number.polestar_VIN_climate_target_temperature'')|int(0)
      }}°C objetivo)'
  - type: grid
    columns: 4
    square: false
    cards:
      - type: tile
        entity: lock.polestar_VIN_lock
        name: Cerradura
        icon: mdi:car-door-lock
        features:
          - type: lock-commands
      - type: tile
        entity: switch.polestar_VIN_climate
        name: Climatización
        icon: mdi:air-conditioner
      - type: tile
        entity: switch.polestar_VIN_charging
        name: Cargar ahora
        icon: mdi:ev-station
      - type: custom:mushroom-template-card
        primary: |-
          {% set loc = 'device_tracker.polestar_VIN_location' %}
          {% if is_state(loc,'home') %}
            En casa
          {% elif state_attr(loc,'latitude') is none %}
            Sin datos
          {% else %}
            {{ distance(loc, 'zone.home')|round(0) }} km
          {% endif %}
        secondary: Ubicación
        icon: mdi:map-marker
        icon_color: >-
          {% if is_state('device_tracker.polestar_VIN_location','home') %}green{%
          else %}blue{% endif %}
        tap_action:
          action: more-info
          entity: device_tracker.polestar_VIN_location
  - type: grid
    columns: 2
    square: false
    cards:
      - type: tile
        entity: number.polestar_VIN_climate_target_temperature
        name: Temp. objetivo
        icon: mdi:thermometer-auto
        color: orange
        features:
          - type: numeric-input
            style: slider
      - type: tile
        entity: number.polestar_VIN_target_soc
        name: SOC objetivo
        icon: mdi:battery-charging-high
        color: green
        features:
          - type: numeric-input
            style: slider
  - type: conditional
    conditions:
      - condition: or
        conditions:
          - condition: state
            entity: binary_sensor.polestar_VIN_tailgate
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_front_left_window
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_front_right_window
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_rear_left_window
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_rear_right_window
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_service_required
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_light_failure
            state: 'on'
          - condition: state
            entity: binary_sensor.polestar_VIN_tyre_warning
            state: 'on'
          - condition: state
            entity: update.polestar_VIN_software_update
            state: 'on'
          - condition: numeric_state
            entity: sensor.polestar_VIN_front_left_tyre_pressure
            below: 265
          - condition: numeric_state
            entity: sensor.polestar_VIN_front_right_tyre_pressure
            below: 265
          - condition: numeric_state
            entity: sensor.polestar_VIN_rear_left_tyre_pressure
            below: 265
          - condition: numeric_state
            entity: sensor.polestar_VIN_rear_right_tyre_pressure
            below: 265
    card:
      type: custom:mushroom-template-card
      primary: Aviso del vehículo
      secondary: |-
        {% set issues = [] %} {% if is_state('binary_sensor.polestar_VIN_tailgate','on') %}{% set issues = issues + ['Maletero abierto'] %}{% endif %} {% if is_state('lock.polestar_VIN_lock','locked') %}
          {% if is_state('binary_sensor.polestar_VIN_front_left_window','on') %}{% set issues = issues + ['Vent. del. izq. abierta'] %}{% endif %}
          {% if is_state('binary_sensor.polestar_VIN_front_right_window','on') %}{% set issues = issues + ['Vent. del. dcho. abierta'] %}{% endif %}
          {% if is_state('binary_sensor.polestar_VIN_rear_left_window','on') %}{% set issues = issues + ['Vent. tras. izq. abierta'] %}{% endif %}
          {% if is_state('binary_sensor.polestar_VIN_rear_right_window','on') %}{% set issues = issues + ['Vent. tras. dcho. abierta'] %}{% endif %}
        {% endif %} {% if is_state('binary_sensor.polestar_VIN_service_required','on') %}{% set issues = issues + ['Revisión requerida'] %}{% endif %} {% if states('sensor.polestar_VIN_brake_fluid_warning') != 'no_warning' %}{% set issues = issues + ['Líquido de frenos'] %}{% endif %} {% if is_state('binary_sensor.polestar_VIN_light_failure','on') %}{% set issues = issues + ['Fallo de luz'] %}{% endif %} {% if is_state('update.polestar_VIN_software_update','on') %}{% set issues = issues + ['Actualización firmware'] %}{% endif %} {% if states('sensor.polestar_VIN_front_left_tyre_pressure')|float(999) < 265 %}{% set issues = issues + ['Presión baja: del. izq.'] %}{% endif %} {% if states('sensor.polestar_VIN_front_right_tyre_pressure')|float(999) < 265 %}{% set issues = issues + ['Presión baja: del. dcho.'] %}{% endif %} {% if states('sensor.polestar_VIN_rear_left_tyre_pressure')|float(999) < 265 %}{% set issues = issues + ['Presión baja: tras. izq.'] %}{% endif %} {% if states('sensor.polestar_VIN_rear_right_tyre_pressure')|float(999) < 265 %}{% set issues = issues + ['Presión baja: tras. dcho.'] %}{% endif %} {{ issues | join(' · ') }}
      icon: mdi:alert-circle
      icon_color: red
  - type: grid
    columns: 2
    square: false
    cards:
      - type: gauge
        entity: sensor.polestar_VIN_battery_level
        name: Batería
        min: 0
        max: 100
        needle: true
        severity:
          red: 0
          yellow: 20
          green: 50
      - type: gauge
        entity: sensor.polestar_VIN_range
        name: Autonomía (km)
        min: 0
        max: 500
        needle: true
        severity:
          red: 0
          yellow: 80
          green: 150
  - type: entities
    title: Carga
    show_header_toggle: false
    entities:
      - entity: sensor.polestar_VIN_charging_power
        name: Potencia (W)
      - entity: sensor.polestar_VIN_time_to_full_charge
        name: Min → 100%
      - entity: switch.polestar_VIN_charge_timer
        name: Temporizador
      - type: conditional
        conditions:
          - entity: switch.polestar_VIN_charge_timer
            state: 'on'
        row:
          entity: time.polestar_VIN_charge_timer_start
          name: Inicio programado
      - type: conditional
        conditions:
          - entity: switch.polestar_VIN_charge_timer
            state: 'on'
        row:
          entity: time.polestar_VIN_charge_timer_stop
          name: Fin programado
  - type: glance
    show_name: true
    show_icon: true
    show_state: true
    columns: 4
    entities:
      - entity: sensor.polestar_VIN_charging_time_domestic_power
        name: 7kW
        icon: mdi:home-lightning-bolt
      - entity: sensor.polestar_VIN_charging_time_low_power
        name: 22kW
        icon: mdi:ev-station
      - entity: sensor.polestar_VIN_charging_time_fast_power
        name: 100kW
        icon: mdi:ev-station
      - entity: sensor.polestar_VIN_charging_time_ultrafast_power
        name: 300kW
        icon: mdi:lightning-bolt-circle
  - type: grid
    columns: 4
    square: false
    cards:
      - type: custom:mushroom-entity-card
        entity: button.polestar_VIN_close_windows
        name: Cerrar ventanas
        icon: mdi:car-door-lock
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_close_windows
      - type: custom:mushroom-entity-card
        entity: button.polestar_VIN_flash_lights
        name: Destellar luces
        icon: mdi:car-light-high
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_flash_lights
      - type: custom:mushroom-entity-card
        entity: button.polestar_VIN_honk
        name: Bocina
        icon: mdi:bugle
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_honk
      - type: custom:mushroom-entity-card
        entity: button.polestar_VIN_honk_and_flash
        name: Localizar
        icon: mdi:car-emergency
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_honk_and_flash
      - type: custom:mushroom-template-card
        primary: >-
          {% if is_state('binary_sensor.polestar_VIN_tailgate','on') %}Abierto{%
          else %}Cerrado{% endif %}
        secondary: Maletero
        icon: mdi:car-back
        icon_color: >-
          {% if is_state('binary_sensor.polestar_VIN_tailgate','on') %}orange{%
          else %}blue{% endif %}
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_unlock_trunk
      - type: custom:mushroom-entity-card
        entity: button.polestar_VIN_refresh
        name: Actualizar
        icon: mdi:refresh
        tap_action:
          action: call-service
          service: button.press
          target:
            entity_id: button.polestar_VIN_refresh
  - type: conditional
    conditions:
      - condition: state
        entity: switch.polestar_VIN_climate
        state: 'on'
    card:
      type: entities
      title: Climatización
      show_header_toggle: false
      state_color: false
      entities:
        - entity: sensor.polestar_VIN_climate_time_remaining
          name: Tiempo restante (min)
          icon: mdi:timer-sand
  - type: custom:mushroom-template-card
    primary: Calefacción asientos y volante
    secondary: Toca para mostrar
    icon: >-
      {% if is_state('input_boolean.polestar_clima_expandido','on') %}mdi:chevron-up
      {% else %}mdi:chevron-down{% endif %}
    icon_color: grey
    tap_action:
      action: call-service
      service: input_boolean.toggle
      target:
        entity_id: input_boolean.polestar_clima_expandido
  - type: conditional
    conditions:
      - condition: state
        entity: input_boolean.polestar_clima_expandido
        state: 'on'
    card:
      type: entities
      show_header_toggle: false
      entities:
        - entity: select.polestar_VIN_climate_steering_wheel_heat
          name: Volante calefactado
          icon: mdi:steering
        - entity: select.polestar_VIN_climate_front_left_seat_heat
          name: Asiento del. izq.
          icon: mdi:seat-recline-extra
        - entity: select.polestar_VIN_climate_front_right_seat_heat
          name: Asiento del. dcho.
          icon: mdi:seat-recline-extra
  - type: glance
    title: Consumo & Viaje
    show_name: true
    show_icon: true
    show_state: true
    columns: 3
    entities:
      - entity: sensor.polestar_VIN_average_consumption_auto
        name: kWh/100km
        icon: mdi:leaf
      - entity: sensor.polestar_VIN_odometer
        name: Odómetro (km)
        icon: mdi:counter
      - entity: sensor.polestar_VIN_outside_temperature
        name: Temp. ext. (°C)
        icon: mdi:thermometer
      - entity: sensor.polestar_VIN_trip_meter_auto
        name: Trip actual
        icon: mdi:map-clock-outline
      - entity: sensor.polestar_VIN_speed
        name: Velocidad
        icon: mdi:speedometer
  - type: glance
    title: Puertas & Ventanas
    show_name: true
    show_icon: true
    show_state: true
    columns: 3
    entities:
      - entity: binary_sensor.polestar_VIN_front_left_door
        name: Del. izq.
        icon: mdi:car-door
      - entity: binary_sensor.polestar_VIN_front_right_door
        name: Del. dcho.
        icon: mdi:car-door
      - entity: binary_sensor.polestar_VIN_rear_left_door
        name: Tras. izq.
        icon: mdi:car-door
      - entity: binary_sensor.polestar_VIN_rear_right_door
        name: Tras. dcho.
        icon: mdi:car-door
      - entity: binary_sensor.polestar_VIN_tailgate
        name: Maletero
        icon: mdi:car-back
      - entity: binary_sensor.polestar_VIN_hood
        name: Capó
        icon: mdi:car-cog
      - entity: binary_sensor.polestar_VIN_front_left_window
        name: Vent. del. izq.
        icon: mdi:window-open
      - entity: binary_sensor.polestar_VIN_front_right_window
        name: Vent. del. dcho.
        icon: mdi:window-open
      - entity: binary_sensor.polestar_VIN_sunroof
        name: Techo solar
        icon: mdi:car-convertible
  - type: conditional
    conditions:
      - condition: state
        entity: device_tracker.polestar_VIN_location
        state_not: home
    card:
      type: map
      entities:
        - entity: device_tracker.polestar_VIN_location
      aspect_ratio: 16x9
  - type: custom:mushroom-template-card
    primary: Servicio & Salud
    secondary: '{{ states(''sensor.polestar_VIN_days_to_service'') }} días ·
      {{ states(''sensor.polestar_VIN_distance_to_service'') }} km · {{ state_attr(''update.unofficial_polestar_update'',''installed_version'')
      }}'
    icon: >-
      {% if is_state('input_boolean.polestar_salud_expandida','on') %}mdi:chevron-up
      {% else %}mdi:chevron-down{% endif %}
    icon_color: >-
      {% if is_state('binary_sensor.polestar_VIN_service_required','on') %}red
      {% elif is_state('binary_sensor.polestar_VIN_tyre_warning','on') %}orange
      {% else %}grey{% endif %}
    tap_action:
      action: call-service
      service: input_boolean.toggle
      target:
        entity_id: input_boolean.polestar_salud_expandida
  - type: conditional
    conditions:
      - condition: state
        entity: input_boolean.polestar_salud_expandida
        state: 'on'
    card:
      type: vertical-stack
      cards:
        - type: grid
          columns: 2
          square: false
          cards:
            - type: gauge
              entity: sensor.polestar_VIN_front_left_tyre_pressure
              name: Del. izq. (kPa)
              min: 240
              max: 380
              needle: true
              severity:
                red: 240
                yellow: 265
                green: 290
            - type: gauge
              entity: sensor.polestar_VIN_front_right_tyre_pressure
              name: Del. dcho. (kPa)
              min: 240
              max: 380
              needle: true
              severity:
                red: 240
                yellow: 265
                green: 290
            - type: gauge
              entity: sensor.polestar_VIN_rear_left_tyre_pressure
              name: Tras. izq. (kPa)
              min: 240
              max: 380
              needle: true
              severity:
                red: 240
                yellow: 265
                green: 290
            - type: gauge
              entity: sensor.polestar_VIN_rear_right_tyre_pressure
              name: Tras. dcho. (kPa)
              min: 240
              max: 380
              needle: true
              severity:
                red: 240
                yellow: 265
                green: 290
        - type: entities
          show_header_toggle: false
          entities:
            - entity: sensor.polestar_VIN_days_to_service
              name: Días para revisión
              icon: mdi:calendar-clock
            - entity: sensor.polestar_VIN_distance_to_service
              name: km para revisión
              icon: mdi:map-marker-path
            - entity: sensor.polestar_VIN_service_warning
              name: Aviso servicio
              icon: mdi:alert-circle
            - entity: sensor.polestar_VIN_brake_fluid_warning
              name: Líquido de frenos
              icon: mdi:car-brake-fluid-level
            - entity: binary_sensor.polestar_VIN_low_voltage_battery
              name: Batería 12V
              icon: mdi:car-battery
            - entity: binary_sensor.polestar_VIN_light_failure
              name: Fallo de luz
              icon: mdi:car-light-alert
            - type: section
              label: Software
            - entity: update.unofficial_polestar_update
              name: Integración Polestar
            - entity: update.polestar_VIN_software_update
              name: Firmware del coche
            - entity: binary_sensor.polestar_VIN_available
              name: API disponible
              icon: mdi:wifi-check
```

### As a library


    python  
    from polestar_api import PolestarApi  
      
    async with PolestarApi(email="you@example.com", password="...") as api:  
     vehicles = await api.get_vehicles() car = vehicles[0]  
     battery = await car.get_battery() print(f"{battery.charge_level}% — {battery.range_km} km")  
     location = await car.get_location() print(f"Lat {location.coordinate.latitude}, Lon {location.coordinate.longitude}") 

## Features

- **Battery** — charge level, range, charging status, power (with real-time streaming)
- **Location** — last known and last parked position (with real-time streaming)
- **Climate** — start/stop climatization with target temperature, seat and steering wheel heating
- **Climate timers** — view and manage scheduled parking climate timers
- **Locks** — lock, unlock, trunk unlock
- **Honk & flash** — flash lights or honk+flash
- **Windows** — open/close all windows
- **Exterior** — door, window, sunroof, hood, tailgate, and alarm status.
- **Charging** — target SOC, amp limit, charge timers, start/stop immediate charging
- **Charge locations** — full CRUD for saved locations with per-location amp limits, min SOC, timers, departure times, and smart charging
- **Health** — service warnings, fluid levels, tyre pressures (kPa), all exterior light warnings, 12V battery
- **Availability** — vehicle online status with unavailable reason
- **Weather** — temperature at car location
- **OTA** — software update info, scheduling, install now, cancel
- **Pre-cleaning** — air quality status (PM2.5, AQI) and start/stop cabin pre-cleaning

For the full API reference with all methods, models, and enums, see the [docs](https://kildahldev.github.io/unofficial-polestar-api/).

### Feature availability by model

Not all features are available on all models. Unsupported commands are
detected at runtime — the corresponding HA entities become unavailable
automatically and re-enable if a future software update adds support.

| Feature | Polestar 2 | Polestar 3 | Polestar 4 |
|---------|:----------:|:----------:|:----------:|
| Battery | ✅ | ✅ | ✅ |
| Location / parked location | ✅ | ✅ | ✅ |
| Climate (start/stop/temp/seats) | ✅ | ✅ | ✅ |
| Climate timers (list) | ✅ | ✅ | ✅ |
| Climate timer settings | ✅ | ✅ | ❌ |
| Lock / unlock | ✅ | ✅ | ✅ |
| Trunk unlock | ✅ | ✅ | ✅ |
| Honk & flash | ✅ | ✅ | ✅ |
| Windows open/close | ✅ | ✅ | ❌ |
| Exterior status | ✅ | ✅ | ✅ |
| Charging start/stop | ✅ | ✅ | ✅ |
| Target SOC | ✅ | ✅ | ✅ |
| Amp limit | ✅ | ✅ | ❌ |
| Charge timer | ✅ | ✅ | ✅ |
| Charge locations | ✅ | ✅ | ✅ |
| Health (tyre pressures) | ❌ (no TPMS) | ✅ | ✅ |
| Availability | ✅ | ✅ | ✅ |
| Weather | ✅ | ✅ | ✅ |
| OTA software info | ✅ | ✅ | ✅ |
| OTA schedule / install / cancel | ✅ | ✅ | ❓ |
| Pre-cleaning | ✅ | ✅ | ✅ |
| Dashboard (legacy PCCS) | ✅ | ❌ | ❌ |
| Connectivity (legacy PCCS) | ✅ | ❌ | ❌ |

> **Note:** Availability was tested on a Polestar 4 (software version as of Jul 2026).
> Polestar 2 and 3 columns are based on community reports and may vary by
> software version. Please report back what works and what doesn't for your model.

## FAQ

**The charging switch doesnt work?**
It uses the same `StartOverride`/`StopOverrideChargeTimer` calls as the official app, which only override an *active* charge schedule. If no schedule is active (the car is charging freely),
there's nothing to override, so the switch has no effect. I have yet to find an API to directly start/stop a charge session.
The only ways to stop a free charge is to set a charge schedule that excludes the current time, or to set the target SOC lower than the current SOC.

## Disclaimer

This project is not affiliated with, endorsed by, or in any way officially connected to Polestar, Volvo Cars, or any of their subsidiaries.

This library does not contain any proprietary code, or copyrighted material from Polestar or Volvo. All code is written from scratch by observing the behaviour of the official app.

All API interactions are based on reverse-engineered, undocumented interfaces. These may change or break without notice. Use at your own risk. The authors are not responsible for any consequences of using this software, including but not limited to vehicle malfunctions, warranty implications, or account restrictions.