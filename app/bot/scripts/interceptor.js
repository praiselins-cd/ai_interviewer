(function() {
    const micCtx = new AudioContext({ sampleRate: 24000 });
    const gainNode = micCtx.createGain();
    gainNode.gain.setValueAtTime(0, micCtx.currentTime);
    const micDest = micCtx.createMediaStreamDestination();
    gainNode.connect(micDest);

    // Chromium suspends a freshly-created AudioContext until resumed
    // (autoplay policy). Headless mode has been observed to be flakier
    // about a single resume() call actually taking effect than headed mode
    // was, so this retries on a short interval until it's confirmed
    // running, rather than relying on one resume() during audio playback.
    console.log(`[Interceptor] AudioContext created, initial state: ${micCtx.state}`);
    const resumeInterval = setInterval(() => {
        if (micCtx.state === 'running') {
            console.log('[Interceptor] Virtual mic AudioContext confirmed running.');
            clearInterval(resumeInterval);
            return;
        }
        micCtx.resume().then(() => {
            console.log(`[Interceptor] resume() called, state now: ${micCtx.state}`);
        }).catch(e => console.warn('[Interceptor] AudioContext resume failed:', e));
    }, 500);

    window.__virtualMicCtx = micCtx;
    window.__virtualMicGainNode = gainNode;
    window.__virtualMicTrack = micDest.stream.getAudioTracks()[0];

    const canvas = document.createElement('canvas');
    canvas.width = 640; canvas.height = 480;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'black';
    ctx.fillRect(0, 0, 640, 480);
    window.__virtualVideoTrack = canvas.captureStream(5).getVideoTracks()[0];

    const originalGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async function(constraints) {
        if (!constraints.audio && !constraints.video) return originalGUM(constraints);
        try {
            const original = await originalGUM(constraints);
            original.getTracks().forEach(t => t.stop());
        } catch (e) { console.warn('GUM blocking suppressed'); }

        const stream = new MediaStream();
        if (constraints.video) stream.addTrack(window.__virtualVideoTrack.clone());
        if (constraints.audio) stream.addTrack(window.__virtualMicTrack.clone());
        return stream;
    };

    // getUserMedia alone isn't enough in headless mode: Teams also calls
    // enumerateDevices() -- a separate API that lists actual hardware --
    // to decide whether the user has a microphone/camera at all, and reacts
    // (e.g. auto-mutes, disables the mic toggle) if that list is empty.
    // Headless Chrome has no real audio/video hardware, so the real
    // enumerateDevices() genuinely returns an empty list; headed mode never
    // hit this because a real machine's real devices show up. Fake device
    // entries here just need the shape Teams actually reads
    // (deviceId/kind/label/groupId) -- not real MediaDeviceInfo instances.
    navigator.mediaDevices.enumerateDevices = async function() {
        return [
            { deviceId: "virtual-mic", kind: "audioinput", label: "Virtual Microphone", groupId: "virtual-group", toJSON() { return this; } },
            { deviceId: "virtual-speaker", kind: "audiooutput", label: "Virtual Speaker", groupId: "virtual-group", toJSON() { return this; } },
            { deviceId: "virtual-camera", kind: "videoinput", label: "Virtual Camera", groupId: "virtual-group", toJSON() { return this; } },
        ];
    };
})();

