// Stand-in for main.py's Api, so index.html runs in a browser (see serve.py). Readings are
// an M1 Pro's, drifting slowly so the charts have shape, with 10 minutes of history.
//
// Query options, combined with commas: ?s=holding,fahrenheit&tab=thermals
//   fan control: off (default) holding max override approval unavailable other error
//   this Mac:    fanless onefan noscreen nochip ioreport hot charging desktop gpuheld (GPU parked 3 min) hint
//                (noscreen,fanless,ioreport is the 13" M4 Air)
//   keep awake:  ka1h ka2h kasleep kaschedule kaofftoday kabattery (paused by the battery rule)
//                kahours (work hours on) karule (battery rule on) sunday (the week starts on Sunday)
//                h24 (a Mac set to 24-hour time) kaday (work hours 0:00–24:00) kanoon (8:00–Noon)
//   displays:    tv (an LG TV answering DDC, with volume) tvnoddc (one that doesn't) tvasking (DDC not answered yet)
//                boosted (the built-in at 130%) noboost (an M5 Pro: stops at 100%) noxdr (no XDR screen: no nits)
//                dkeys (the keys switch on, allowed) dkeyswaiting (on, Accessibility not allowed) muted
//                oldmacos (before macOS 27: the list is still called Accessibility)
//                nodisplays (a build without libdisplays.dylib)
//   mouse:       mouseon (all three switches on, allowed, tap running) mousewaiting (switches on,
//                Accessibility not allowed) mousedenied (all off, not allowed) nomouse (built
//                without a Swift compiler) macos26 (the list is Accessibility, not macOS 27's Device
//                Control and Data Access). Turning a switch on in the preview allows it.
//   settings:    fahrenheit dark light
//   tab:         dashboard (default) thermals keepawake power displays mouse history settings
// Change window.__state while it runs to try anything else; the next poll picks it up.
// window.__state.size is what the page last asked the window to be (the app sizes its
// window to it), window.__state.calls every API call the page made, and window.__state.haptics
// how many trackpad taps it asked for (a brightness slider landing on 100%). Page errors that
// the app would log go to the console.
(() => {
  const app = window.__app;
  const wave = (t, base, amp, period) => Math.round((base + amp * Math.sin(t / period)) * 10) / 10;
  // accessibility.setting_name(): what System Settings calls the list the tap is allowed in.
  const SETTING = /(^|,)macos26(,|$)/.test(new URLSearchParams(location.search).get('s') || '')
    ? 'Accessibility' : 'Device Control and Data Access';
  const state = window.__state = {
    settings: { ...app.settings, launch_at_login: true },
    fanControl: { profile: 'macos', state: 'off' },
    hot: 0,  // added to every temperature
    power: {
      has_battery: true, has_smc: true, is_charging: false, fully_charged: false, external_connected: false,
      battery_level: 63, charge_limit: 80, at_charge_limit: false, time_remaining_min: 254, system_in: 0,
      adapter_power: 0, efficiency_loss: 0, has_screen_power: true, has_chip_power: true, chip_source: 'phpc', temperature: 30.1, cycle_count: 212,
      design_capacity: 6075, max_capacity: 5402, current_capacity: 3401, health_pct: 88.9, voltage: 12.4,
      amperage: -0.76, adapter: null,
    },
    available: ['cpu', 'gpu', 'memory', 'ssd', 'ambient', 'fans'],
    sensors: { cpu: 10, gpu: 2, memory: 6, ssd: 3, ambient: 1 },
    gpuHeldFor: null,  // seconds the GPU has been parked, showing its last reading; null: it isn't
    sensorHint: false,  // Settings asks for a sensor report
    fans: [{ min: 1499, max: 4296 }, { min: 1499, max: 4744 }],
    fanRpm: null,  // [rpm, …] to show; null follows fan control (0 while macOS has them cool)
    keepAwake: { choice: 'off', holding: false, line: null },  // keepawake.KeepAwake.status()
    // displays.Displays.status(): the built-in XDR display at macOS's own maximum to start with.
    displays: {
      available: true, allowed: false, running: false, setting: 'Device Control and Data Access',  // 'Accessibility' before macOS 27 (s=oldmacos)
      displays: [{ id: 1, name: 'Built-in Display', builtin: true, adjustable: true, answered: true, percent: 100, top: 160,
                   nits: 500, volume: null, muted: null, boosting: false }],
    },
    mouse: { available: true, allowed: true, running: false, setting: SETTING },  // mouse.Mouse.status()
    firstWeekday: 0,  // Python's weekday the Mac's calendar starts on: 0 Monday, 6 Sunday
    h24: false,  // the Mac shows 24-hour time (the bar's labels come from keepawake.half_hour_labels)
    sessions: [],
    update: ['idle', null],
    updateUnavailable: null,
    size: null,
    calls: [],
  };

  const scenarios = {
    holding: () => { state.hot = 14; state.fanControl = { profile: 'balanced', state: 'holding', targets: [2310, 2560] }; },
    max: () => { state.fanControl = { profile: 'max', state: 'holding', targets: state.fans.map(f => f.max) }; },
    override: () => { state.hot = 30; state.fanControl = { profile: 'balanced', state: 'override', reason: 'pressure', pressure: 2, targets: [4296, 4744] }; },
    approval: () => { state.fanControl = { profile: 'balanced', state: 'approval' }; },
    unavailable: () => { state.fanControl = { profile: 'balanced', state: 'unavailable', reason: 'until Mac Engine Room is in your Applications folder' }; },
    other: () => { state.fanControl = { profile: 'balanced', state: 'other' }; state.fanRpm = [3000, 3000]; },
    error: () => { state.fanControl = { profile: 'balanced', state: 'error', reason: "it couldn't reach the fan helper" }; },
    fanless: () => { state.available = state.available.filter(k => k !== 'fans'); state.fans = []; state.fanControl = { profile: 'macos', state: 'nofans' }; },
    onefan: () => { state.fans = state.fans.slice(0, 1); },
    noscreen: () => { state.power.has_screen_power = false; },
    nochip: () => { state.power.has_chip_power = false; },  // with noscreen: an M4 Air without IOReport
    ioreport: () => { state.power.chip_source = 'ioreport'; },  // Chip from IOReport, where PHPC doesn't work
    // A Mac mini: no battery, no screen; its PDTR makes it look plugged in.
    desktop: () => Object.assign(state.power, {
      has_battery: false, external_connected: true, battery_level: null, charge_limit: null, time_remaining_min: null,
      has_screen_power: false, temperature: null, cycle_count: null, design_capacity: null, max_capacity: null,
      current_capacity: null, health_pct: null, voltage: null, amperage: null,
    }),
    gpuheld: () => { state.gpuHeldFor = 180; },
    hint: () => { state.sensorHint = true; },
    hot: () => { state.hot = 35; },
    charging: () => Object.assign(state.power, {
      is_charging: true, external_connected: true, time_remaining_min: 64, adapter_power: 52.3,
      adapter: { name: '96W USB-C Power Adapter', watts: 96 }, amperage: 2.4,
    }),
    ka1h: () => { state.keepAwake = keepAwake('1h'); },
    ka2h: () => { state.keepAwake = { choice: '2h', holding: true, line: 'Keep Awake ends at 3:22 PM (1 hour 59 minutes)' }; },
    kasleep: () => { state.keepAwake = keepAwake('sleep'); },
    kaschedule: () => { state.keepAwake = { choice: null, holding: true, line: 'Keep Awake ends at 5 PM (4 hours 38 minutes)' }; },
    kaofftoday: () => { state.keepAwake = { choice: 'off', holding: false, line: null }; },
    kabattery: () => { state.keepAwake = { choice: '2h', holding: false, line: 'Paused because the battery is below 20%' }; },
    kahours: () => { state.settings.keep_awake_hours = { ...state.settings.keep_awake_hours, on: true }; },
    karule: () => { state.settings.keep_awake_battery = { ...state.settings.keep_awake_battery, on: true }; },
    sunday: () => { state.firstWeekday = 6; },
    h24: () => { state.h24 = true; },
    kaday: () => { state.settings.keep_awake_hours = { ...state.settings.keep_awake_hours, start: '00:00', end: '24:00' }; },
    kanoon: () => { state.settings.keep_awake_hours = { ...state.settings.keep_awake_hours, start: '08:00', end: '12:00' }; },
    tv: () => state.displays.displays.push({ id: 5, name: 'LG TV SSCR2', builtin: false, adjustable: true, answered: true,
                                             percent: 60, top: 100, nits: null, volume: 25, muted: false, boosting: false }),
    tvnoddc: () => state.displays.displays.push({ id: 5, name: 'LG TV SSCR2', builtin: false, adjustable: false, answered: false,
                                                  percent: null, top: 100, nits: null, volume: null, muted: null, boosting: false }),
    tvasking: () => state.displays.displays.push({ id: 5, name: 'LG TV SSCR2', builtin: false, adjustable: false, answered: null,
                                                   percent: null, top: 100, nits: null, volume: null, muted: null, boosting: false }),
    boosted: () => setBrightness(1, 130),
    noboost: () => Object.assign(state.displays.displays[0], { top: 100 }),
    noxdr: () => Object.assign(state.displays.displays[0], { top: 100, nits: null }),
    dkeys: () => { state.settings.display_keys = true; Object.assign(state.displays, { allowed: true, running: true }); },
    dkeyswaiting: () => { state.settings.display_keys = true; },
    muted: () => { const tv = state.displays.displays.find(d => d.id === 5); if (tv) tv.muted = true; },
    nodisplays: () => { state.displays = { available: false, displays: [], allowed: false, running: false, setting: 'Device Control and Data Access' }; },
    oldmacos: () => { state.displays.setting = 'Accessibility'; },
    mouseon: () => {
      Object.assign(state.settings, { mouse_reverse_scrolling: true, mouse_smooth_scrolling: true, mouse_back_forward: true });
      state.mouse = { available: true, allowed: true, running: true, setting: SETTING };
    },
    mousewaiting: () => {
      Object.assign(state.settings, { mouse_reverse_scrolling: true });
      state.mouse = { available: true, allowed: false, running: false, setting: SETTING };
    },
    mousedenied: () => { state.mouse = { available: true, allowed: false, running: false, setting: SETTING }; },
    nomouse: () => { state.mouse = { available: false, allowed: false, running: false, setting: SETTING }; },
    macos26: () => {},  // SETTING reads it
    fahrenheit: () => { state.settings.temperature_unit = 'fahrenheit'; },
    dark: () => { state.settings.theme = 'dark'; },
    light: () => { state.settings.theme = 'light'; },
  };

  // What fans.py would report once a profile is chosen on the page: Max at full speed, the
  // others at the fans' minimum (the preview doesn't run their curves).
  function choose(pid) {
    if (!state.fans.length) state.fanControl = { profile: pid, state: 'nofans' };
    else if (pid === 'macos') state.fanControl = { profile: pid, state: 'off' };
    else state.fanControl = { profile: pid, state: 'holding', targets: state.fans.map(f => pid === 'max' ? f.max : f.min) };
  }

  // What keepawake.py would say right after a pick (the preview doesn't run work hours or count down).
  function keepAwake(choice) {
    return {
      off: { choice: 'off', holding: false, line: null },
      '1h': { choice: '1h', holding: true, line: 'Keep Awake ends at 1:22 PM (1 hour)' },
      '2h': { choice: '2h', holding: true, line: 'Keep Awake ends at 2:22 PM (2 hours)' },
      sleep: { choice: 'sleep', holding: true, line: 'Keep Awake ends when the Mac sleeps' },
    }[choice];
  }

  // What displays.py would say after a slider: nits as Native/Displays/Scale.swift works them out
  // (below 100%, 500 × a stand-in for macOS's linear brightness).
  function setBrightness(id, percent) {
    const d = state.displays.displays.find(x => x.id === id);
    if (!d) return;
    d.percent = Math.max(0, Math.min(d.top, percent));
    d.boosting = d.builtin && d.percent > 100;
    if (d.builtin && d.nits !== null) {
      const nits = d.percent <= 100 ? 500 * (d.percent / 100) ** 2.2 : Math.min(1600, 500 * (d.percent / 100) ** 2.5);
      d.nits = nits >= 100 ? Math.round(nits / 10) * 10 : Math.round(nits);  // as Scale.shown rounds them
    }
  }

  function reading(t) {
    const p = state.power, h = state.hot;
    const system = wave(t, 9.4, 3, 40), screen = p.has_screen_power ? wave(t, 2.3, 0.4, 70) : null;
    const chip = p.has_chip_power ? wave(t, 1.8, 1.2, 25) : null;
    const held = ['holding', 'override', 'other'].includes(state.fanControl.state);
    const rpm = state.fanRpm || (held ? state.fanControl.targets || state.fans.map(f => f.min) : state.fans.map(() => 0));
    return {
      power: {
        ...p, t, system_load: system, screen_power: screen, chip_power: chip,
        system_in: p.external_connected ? wave(t, p.has_battery ? 52.3 : system * 1.1, 2, 30) : 0,
        battery_power: !p.has_battery ? null : p.is_charging ? wave(t, 40, 2, 30) : -system,
      },
      thermals: {
        t, cpu: wave(t, 52.4 + h, 6, 90), gpu: wave(t, 48.1 + h, 4, 120), memory: wave(t, 47 + h * 0.8, 2, 200),
        ssd: wave(t, 35.5 + h * 0.4, 1, 300), ambient: wave(t, 26.3 + h * 0.2, 0.5, 400),
        available: state.available, sensors: state.sensors,
        fans: state.fans.map((f, i) => ({ ...f, rpm: rpm[i] ?? 0, manual: held })),
        ...(state.gpuHeldFor !== null && { gpu: null, gpu_held: { c: 47.9 + h, t: t - state.gpuHeldFor, sensors: state.sensors.gpu } }),
        get hottest() { return Math.max(this.cpu, this.gpu, this.memory, this.ssd); },
      },
    };
  }

  const HISTORY = 600;  // seconds of points before the page's first poll
  let seq = 0;
  // What mouse.py would report after the switches change (the preview pretends Accessibility is allowed
  // once a switch is turned on, as if the maintainer allowed it at macOS's prompt).
  function mouseAfter(settings) {
    const on = settings.mouse_reverse_scrolling || settings.mouse_smooth_scrolling || settings.mouse_back_forward;
    if (!state.mouse.available) return;
    if (on) state.mouse.allowed = true;
    state.mouse.running = on && state.mouse.allowed;
  }

  function getLive(sinceSeq, page) {
    const now = Date.now() / 1000, points = [], tpoints = [];
    const times = sinceSeq ? [now] : Array.from({ length: HISTORY }, (_, i) => now - HISTORY + 1 + i);
    let r;
    for (const t of times) {
      r = reading(t);
      const p = r.power, th = r.thermals;
      points.push([Math.round(t * 100) / 100, p.system_load, p.system_in, p.battery_power, p.screen_power, p.chip_power]);
      tpoints.push([Math.round(t * 100) / 100, th.cpu, th.gpu, th.memory, th.ssd, th.ambient]);
    }
    seq += times.length;
    return {
      seq, latest: r.power, point_fields: ['t', 'system_load', 'system_in', 'battery_power', 'screen_power', 'chip_power'],
      points, thermals: { ...r.thermals },  // a plain object, as the bridge sends
      thermal_fields: ['t', 'cpu', 'gpu', 'memory', 'ssd', 'ambient'], thermal_points: tpoints,
      update: state.update, fan_control: state.fanControl, keep_awake: state.keepAwake,
      ...(page === 'displays' && { displays: JSON.parse(JSON.stringify(state.displays)) }),
      ...(page === 'mouse' && { mouse: { ...state.mouse } }),
    };
  }

  const handlers = {
    get_live: getLive,
    get_settings: () => state.settings,
    keep_awake: choice => (state.keepAwake = keepAwake(choice) || state.keepAwake),
    save_settings: changes => {
      const before = state.settings.fan_profile;
      Object.assign(state.settings, changes);
      if (changes.fan_profile && changes.fan_profile !== before) choose(changes.fan_profile);
      if ('display_keys' in changes) state.displays.running = changes.display_keys && state.displays.allowed;
      mouseAfter(state.settings);
      return state.settings;
    },
    app_info: () => ({
      version: app.version, log_path: '/Users/you/Library/Logs/Mac Engine Room/app.log',
      history_recording: state.settings.record_history, update_unavailable_reason: state.updateUnavailable,
      fan_profiles: app.fan_profiles, fan_tuning_limits: app.fan_tuning_limits, sensor_hint: state.sensorHint,
      keep_awake_choices: app.keep_awake_choices, keep_awake_battery_levels: app.keep_awake_battery_levels,
      first_weekday: state.firstWeekday, clock: state.h24 ? app.clock24 : app.clock12, display_snap: app.display_snap,
    }),
    set_display_brightness: (id, percent, haptic) => { if (haptic) state.haptics = (state.haptics || 0) + 1; setBrightness(id, percent); },
    set_display_volume: (id, percent) => { const d = state.displays.displays.find(x => x.id === id); if (d) Object.assign(d, { volume: percent, muted: false }); },
    refresh_displays: () => null,
    open_accessibility_settings: () => null,
    copy_sensor_report: () => new Promise(done => setTimeout(() => done(true), 300)),
    report_problem: () => null,
    open_pointer_settings: () => null,
    open_accessibility_settings: () => null,
    check_for_updates: () => { state.update = ['checking', null]; setTimeout(() => { state.update = ['idle', null]; }, 2000); },
    list_sessions: () => state.sessions,
    get_session: () => null,
    clear_history: () => { const n = state.sessions.length; state.sessions = []; return n; },
    set_content_size: (w, h, fit) => { state.size = { width: w, height: h, fit }; },
    log_error: message => console.error('Dashboard error:', message),
  };

  window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
    state.calls.push([name, ...args]);
    return handlers[name] ? handlers[name](...args) ?? null : null;
  } }) };

  window.__preview = {
    start() {
      const q = new URLSearchParams(location.search);
      for (const s of (q.get('s') || '').split(',').filter(Boolean)) {
        if (scenarios[s]) scenarios[s]();
        else console.error(`Preview: no scenario "${s}". Scenarios: ${Object.keys(scenarios).join(', ')}`);
      }
      state.settings.fan_profile = state.fanControl.profile;
      window.addEventListener('pywebviewready', () => {
        const tab = q.get('tab');
        if (tab) setTimeout(() => showTab(tab), 100);
      });
      setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 0);
    },
  };
})();
