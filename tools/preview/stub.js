// Stand-in for main.py's Api, so index.html runs in a browser (see serve.py). Readings are
// an M1 Pro's, drifting slowly so the charts have shape, with 10 minutes of history.
//
// Query options, combined with commas: ?s=holding,fahrenheit&tab=thermals
//   fan control: off (default) holding max override approval unavailable other error
//   this Mac:    fanless onefan noscreen nochip ioreport hot charging desktop gpuheld (GPU parked 3 min) hint
//                (noscreen,fanless,ioreport is the 13" M4 Air)
//   settings:    fahrenheit dark light
//   tab:         dashboard (default) thermals power history settings
// Change window.__state while it runs to try anything else; the next poll picks it up.
// window.__state.size is what the page last asked the window to be (the app sizes its
// window to it), and window.__state.calls every API call the page made. Page errors that
// the app would log go to the console.
(() => {
  const app = window.__app;
  const wave = (t, base, amp, period) => Math.round((base + amp * Math.sin(t / period)) * 10) / 10;
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
  function getLive(sinceSeq) {
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
      update: state.update, fan_control: state.fanControl,
    };
  }

  const handlers = {
    get_live: getLive,
    get_settings: () => state.settings,
    save_settings: changes => {
      const before = state.settings.fan_profile;
      Object.assign(state.settings, changes);
      if (changes.fan_profile && changes.fan_profile !== before) choose(changes.fan_profile);
      return state.settings;
    },
    app_info: () => ({
      version: app.version, log_path: '/Users/you/Library/Logs/Mac Engine Room/app.log',
      history_recording: state.settings.record_history, update_unavailable_reason: state.updateUnavailable,
      fan_profiles: app.fan_profiles, fan_tuning_limits: app.fan_tuning_limits, sensor_hint: state.sensorHint,
    }),
    copy_sensor_report: () => new Promise(done => setTimeout(() => done(true), 300)),
    report_problem: () => null,
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
