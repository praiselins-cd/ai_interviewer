// Remove webdriver property
Object.defineProperty(navigator, 'webdriver', {
  get: () => false,
});

// Remove headless chrome signature
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5],
});

// Mask chrome version
Object.defineProperty(navigator, 'vendor', {
  get: () => 'Google Inc.',
});

// Prevent chrome detection
window.chrome = {
  runtime: {}
};

// Override the permissions check to prevent bot detection
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);

// Add realistic history
if (window.history.length === 1) {
  window.history.pushState({}, '', '/');
}

// Mask playwright detection in console
if (window.console) {
  const originalLog = console.log;
  console.log = function(...args) {
    const filtered = args.map(arg => {
      if (typeof arg === 'string' && arg.includes('playwright')) {
        return arg.replace(/playwright/gi, 'browser');
      }
      return arg;
    });
    originalLog.apply(console, filtered);
  };
}
