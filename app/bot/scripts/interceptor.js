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

    // 1280x720 @ 15fps to satisfy Teams' own exact/min-max getUserMedia
    // constraints (confirmed via logging: Teams requests width min=max=1280,
    // height min=max=720, frameRate min=15/max=30) -- a 640x480 @5fps track
    // doesn't match what Teams explicitly asked for, which is why the video
    // stayed black even though the canvas itself was drawing correctly.
    const CANVAS_WIDTH = 1280, CANVAS_HEIGHT = 720;
    const canvas = document.createElement('canvas');
    canvas.width = CANVAS_WIDTH; canvas.height = CANVAS_HEIGHT;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'black';
    ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT);
    window.__virtualVideoTrack = canvas.captureStream(15).getVideoTracks()[0];

    // Swaps the plain black frame for the avatar image once it loads. The
    // canvas is never cleared/redrawn elsewhere, so captureStream() just
    // keeps re-encoding whatever's currently on it -- drawing here, even a
    // moment after captureStream() started, is enough to make this the
    // bot's permanent "video".
    console.log(`[Interceptor] Avatar data URI length: ${"__AVATAR_DATA_URI__".length}`);
    const avatarImg = new Image();
    let imgLoaded = false;
    avatarImg.onload = () => {
        imgLoaded = true;
        console.log(`[Interceptor] Avatar image loaded (${avatarImg.width}x${avatarImg.height}).`);
    };
    avatarImg.onerror = (e) => console.warn('[Interceptor] Avatar image failed to load (possibly blocked by page CSP):', e);
    avatarImg.src = "__AVATAR_DATA_URI__";

    // Continuously draw to canvas so captureStream() produces active motion/frames.
    // Chromium WebRTC video track encoders frequently drop static canvas streams if 
    // no new frames are drawn after initial capture.
    function drawFrame() {
        if (imgLoaded) {
            const scale = Math.max(CANVAS_WIDTH / avatarImg.width, CANVAS_HEIGHT / avatarImg.height);
            const w = avatarImg.width * scale, h = avatarImg.height * scale;
            ctx.drawImage(avatarImg, (CANVAS_WIDTH - w) / 2, (CANVAS_HEIGHT - h) / 2, w, h);
        } else {
            ctx.fillStyle = 'black';
            ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT);
        }
        requestAnimationFrame(drawFrame);
    }
    drawFrame();

    // Diagnostic: samples the canvas's own pixel data directly (bypassing
    // getUserMedia/MediaStreamTrack entirely) so we can tell whether the
    // canvas itself genuinely holds non-black content over time, independent
    // of anything Teams or the track/stream pipeline might be doing to it.
    // A SecurityError here would mean the canvas got tainted (blocks reading
    // pixels back) -- unexpected for a data: URI image, but worth ruling out.
    setInterval(() => {
        try {
            const p = ctx.getImageData(CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2, 1, 1).data;
            console.log(`[Interceptor] Canvas center pixel RGBA: ${p[0]},${p[1]},${p[2]},${p[3]}`);
        } catch (e) {
            console.warn('[Interceptor] Could not read canvas pixel data:', e);
        }
    }, 4000);

    let gumCallCount = 0;
    const originalGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async function(constraints) {
        gumCallCount++;
        console.log(`[Interceptor] getUserMedia call #${gumCallCount} with constraints: ${JSON.stringify(constraints)}`);
        if (!constraints.audio && !constraints.video) return originalGUM(constraints);
        try {
            const original = await originalGUM(constraints);
            original.getTracks().forEach(t => t.stop());
        } catch (e) { console.warn('GUM blocking suppressed'); }

        const stream = new MediaStream();
        if (constraints.video) {
            const clonedTrack = window.__virtualVideoTrack.clone();
            stream.addTrack(clonedTrack);
            // Diagnostic: confirms the clone is actually "live" and what
            // resolution/frameRate it's reporting, since the video showing
            // black despite the canvas drawing correctly means the problem
            // is somewhere between this track and what Teams does with it.
            console.log(
                `[Interceptor] Video track cloned: readyState=${clonedTrack.readyState}, `
                + `muted=${clonedTrack.muted}, settings=${JSON.stringify(clonedTrack.getSettings())}`
            );
        }
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

