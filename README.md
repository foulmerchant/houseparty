# HouseParty

> **DO NOT USE — IN TESTING.** Experimental code only. Do not rely on this integration for safety, security, or property protection.

A self-contained alarm system for Home Assistant: an integration that *is* the alarm, and a card to run it.

> **Status: HA test candidate.** All 38 Python tests and 30 Rust core tests pass. Strict Rust linting, WASM release compilation, TypeScript checks, and npm vulnerability audits pass. Home Assistant wiring and the card still require runtime testing in a real HA instance; do not use this as your only alarm yet.

## Why an integration, not just a card

A dashboard card only runs while someone has a dashboard open. An alarm has to watch sensors, count down delays and sound when nobody is looking, so HouseParty's engine runs inside Home Assistant. That also puts code checks and lockouts on the server, where no script in a browser can skip them.

## Layout

```
custom_components/houseparty/   The integration (Python, stdlib only)
  engine.py      The alarm state machine. No HA imports; fully unit-tested.
  auth.py        Users, scrypt-hashed codes, duress, per-HA-user lockout.
  options.py     Settings -> engine config, with clamping and validation.
  alarm_control_panel.py   The entity: wires the engine to HA.
  config_flow.py Setup (requires a first user and code) and settings menus.
  store.py       Users in a private, owner-only storage file.
tests/           python3 -m unittest discover -s tests   (no HA needed)
crates/houseparty-core      Card logic in Rust, zero dependencies.
crates/houseparty-card-ui   Leptos UI compiled to WebAssembly.
shim/                       The card's custom element and safety gate.
```

## Features

Adopted from what a full alarm system needs (and what Alarmo offers):

- **Five arm modes** (away, home, night, vacation, custom), each with its own exit delay, entry delay and alarm time.
- **Sensors per mode.** You choose which sensors protect each mode. Per-sensor behavior: trigger immediately (no entry delay); may be open when arming (motion); trigger during the exit delay (not on your way out); trigger if it goes offline.
- **Always-on sensors** (tamper, smoke, water) trigger even when disarmed.
- **Refuses to arm** if any sensor in the mode is open *or not reporting*.
- **Force-arm with exact bypass.** You approve the specific sensors to bypass. If anything else opens before arming, HouseParty refuses and shows the new list.
- **Users with their own codes**, each limited to certain modes and to disarming or not. Codes are 6–12 digits and unique. At most 10 enabled users per alarm bound server-side scrypt work.
- **Duress codes.** They disarm normally on every screen, but fire `houseparty_duress` for your automations.
- **Events** for automations: `houseparty_armed`, `_disarmed`, `_failed_to_arm`, `_entry`, `_triggered`, `_trigger_ended`, `_failed_code`, `_duress`, `_sensor_lost`.
- **Multiple areas.** Add the integration once per area; each is its own alarm.

## Install

1. Copy `custom_components/houseparty` into your HA `config/custom_components/` and restart.
2. **Settings → Devices & services → Add integration → HouseParty.** Name the alarm and create the first user and code.
3. Open HouseParty's **Configure** menu to choose modes and delays, sensors per mode, sensor behavior, and more users.
4. Add the card (below).

The integration and the card are separate HACS repositories (HACS takes one category per repo), so split them before publishing.

### The card

**Serve Home Assistant over HTTPS.** The card checks its WASM file's hash before running it. All alarm controls require a secure browser context; over plain HTTP the card is status-only and will not send a code. The plain-DOM fallback follows the same rule.

Build with `./scripts/build.sh`, copy `dist/houseparty-card.js` and `dist/houseparty-card_bg.wasm` into `config/www/houseparty/`, and add `/local/houseparty/houseparty-card.js` as a JavaScript module resource.

```yaml
type: custom:houseparty-card
entity: alarm_control_panel.house
privacy: true                 # default: who/when and sensor details stay hidden until "Show details"
show_entry_countdown: false   # default: during entry delay, ring only, no seconds
```

### Idle screensaver

For an old phone or tablet used as the panel, the card can loop GIFs or clips while nobody's using it. The first tap only wakes it.

```yaml
idle:
  media:
    - /local/houseparty/idle/otter.mp4
    - /local/houseparty/idle/kitten.webp
    - /local/houseparty/idle/puppy.gif
  after: 120     # seconds without a touch before it starts (30–3600, default 120)
  rotate: 60     # seconds per clip (10–3600, default 60)
```

Put the files in `config/www/houseparty/idle/`. Some rules, all on purpose:

- **Local files only.** Remote URLs (Giphy, Reddit) are rejected. They'd put a third party on your alarm panel, track every view, show whatever they like, and stop working when your internet does. Anything in `config/www` can be fetched by anyone on your network without logging in, so keep it to cute animals, not family photos.
- **It never hides anything that matters.** It only appears when the alarm is disarmed or fully armed, the connection is proven live, and there's no error, pending command or code entry. Arming, entry delay, triggered, a lost connection or any state change wakes it instantly. A small status chip stays on screen, moving corner to corner to avoid burn-in.
- **The waking tap can't press a button.** The overlay is still there when the tap lands, and controls ignore taps for 0.6 s after waking.
- **Prefer MP4 or WebM over GIF.** Video is decoded by the hardware; animated GIFs are decoded frame by frame on the CPU, which drains and heats old phones. To convert:
  `ffmpeg -i in.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -an out.mp4`
- **Nothing loads until it's needed.** Media is mounted only while showing and released on wake. With "reduce motion" set on the device, video shows a still frame; GIFs can't be paused, another reason to use video.

### Motion and feedback

```yaml
animations: auto   # auto (default) | full | subtle | off
haptics: true      # short vibrations on press, success and error (Android; iOS ignores)
```

What moves, and what it means:

| Moment | Feedback |
|---|---|
| Tap a button or key | Press-in scale, a light tap vibration |
| Command sent | The ring dims and a comet orbits it, with "Waiting for Home Assistant to confirm…" |
| HouseParty confirms a new state | The ring recolors and one bloom ripples out in the new state's color; the label eases in; a short vibration |
| Refused, wrong code or no confirmation | The message shakes once; a double vibration |
| Code entry | Dots pop in a left-to-right ripple on each press (still a fixed six, so length isn't revealed) |
| Exit delay | The ring drains in real time |
| Entry delay | The label breathes slowly; the ring drains (no seconds shown, by default) |
| Triggered | Two alarm waves roll out from the ring, half a beat apart |
| Checking connection | A small spinner |
| Screensaver | Fades in slowly; waking is instant |

Two rules hold throughout, both for safety. **Nothing celebrates a state HouseParty hasn't confirmed.** The bloom is driven by reported state changes, never by taps. And **motion never delays information.** Text is present from the first frame, alerts never fade in from invisible, and nothing waits on an exit animation.

For performance, it's all CSS: transform and opacity only, no animation library, no JavaScript animation loop. Flashes stay at or under once per second. Under the screensaver the card's own animations pause. `auto` drops to `subtle` (fades and press feedback, no blooms or waves) on low-end devices (two cores or fewer, or 1 GB of memory or less). The device's "reduce motion" setting always wins: everything stops except the exit/entry drain, which is information rather than decoration. "Sending" then shows as a still arc, and triggered as steady red.

Sensors and delays live in HouseParty, not the card. Old `sensors`, `exit_delay`, `entry_delay` and `sensor_source` settings are rejected with an explanation.

## Security model

### What HouseParty enforces (server-side)

- **A first user and code are required at setup.** An alarm without codes would be one anyone with dashboard access could disarm.
- **Codes are hashed with scrypt** (salted, about 16 MiB and tens of milliseconds per check) and stored in a private, owner-only file, separate from config entries and diagnostics. To be honest about the limit: 6 digits is a million guesses, so anyone who can read HA's storage could brute-force them offline. The hash stops casual reading; access to `/config` is what really protects them. Prefer 8+ digits.
- **Every code check tests every user,** so the time taken doesn't reveal whose code it was.
- **Lockout per HA user.** Three free tries, then 30 s, doubling to 15 minutes. It's keyed by the HA user making the call, so someone hammering the wall tablet locks out the tablet's account, not your phone. Code checks are serialized, so parallel guesses can't slip past it. A valid code used for something it isn't allowed to do counts as a failure, so "not allowed" can't confirm a guess.
- **HA's per-entity "default code" is ignored,** with a warning in the log if one is set. With it, anyone could disarm without a code.
- **Fail closed:** sensors that aren't reporting block arming. A restart during an entry delay or alarm comes back *triggered*. A restart during the exit delay restarts it. A door opened while HA was down is caught on startup. A sensor going offline while it's protecting something always fires `houseparty_sensor_lost`, and can be set to trigger.
- **No dependencies:** Python standard library only.

### What the card does

- Talks only to HouseParty entities (it checks the `houseparty_api` attribute), and only through alarm arm/disarm services or `houseparty.arm`, pinned to the configured entity.
- Shows a state only on a proven-live connection: a shared heartbeat pings HA every 10 s while visible.
- Shows "waiting for confirmation" until HouseParty reports the expected state, never the hoped-for one.
- Shows HouseParty's refusal list, and force-arms with exactly what you approved.
- Privacy mode by default: who changed it, bypassed sensors and what triggered it stay hidden until tapped.
- Shuffled keypad, fixed-width code dots, password-manager opt-outs, and code entry cleared after 20 s idle or on screen sleep.
- Error banners expire after 60 s.
- A hash-checked, cached WASM binary, with a plain-DOM fallback if it fails. The fallback allows Disarm only over HTTPS; insecure contexts are status-only.

### What you have to do

- **Every frontend plugin is trusted with your code.** All cards and themes share the page. Keep wall tablets minimal and audit HACS installs.
- **Lock down wall tablets:** a dedicated non-admin HA user, kiosk mode, and the kiosk app's remote admin off or strongly protected.
- **Keep a second way to disarm** (phone, keyfob). A lockout on the tablet's account is real during an emergency.
- **Automate on the events,** especially `houseparty_failed_code`, `houseparty_duress` and `houseparty_sensor_lost`.
- **Protect `/config`.** Write access to it means access to the alarm.

## Performance

- **Server:** sensor changes that don't affect the alarm cause no state write. Sensor lookups are indexed. Code hashing runs off the event loop. One timer per alarm, only while a delay or alarm time is running.
- **Card:** watches only the alarm entity, diffed by identity. One heartbeat per page. Updates and timers pause while hidden. CSS-driven animation, memoized views, and no-op updates skipped.
- **Trade-offs taken for security:** each code check costs tens of milliseconds *per user*, so a code check takes longer the more users you have (about 0.5 s with 10 users on a Raspberry Pi 4). The card's WASM is hash-checked before compiling, so it can't stream-compile or reuse the browser's compiled-code cache.

## Before a first release

- Run it in a real HA instance, starting with `code_or_default_code` (verify the override still applies on your HA version), restart/restore behavior, code entry, and reconnect handling.

## Not done yet

A panel for managing settings beyond HA's options forms, per-area dashboards, notifications built in (use the events), and camera snapshots on trigger.
