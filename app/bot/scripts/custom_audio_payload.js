(function() {
    console.log("[AudioPayload] Starting Realtime Bridge...");

    const socket = new WebSocket(`ws://localhost:${window.__VOICE_BRIDGE_PORT__}`);
    socket.binaryType = 'arraybuffer';

    socket.onopen = () => console.log('[AudioPayload] ✅ WebSocket connected to Python.');
    socket.onerror = (e) => console.log('[AudioPayload] ❌ WebSocket error:', e);
    socket.onclose = (e) => console.log('[AudioPayload] 🔴 WebSocket closed. Code:', e.code, e.reason);

    const virtualMicCtx = window.__virtualMicCtx; 
    const gainNode = window.__virtualMicGainNode;
    let nextPlayTime = 0;
    // Set default output volume to 0 (muting by default until Python validates the transcript)
    gainNode.gain.setValueAtTime(1, virtualMicCtx.currentTime);

    // PLAYBACK: AI -> Meeting
    socket.onmessage = async (event) => {
        if (typeof event.data === 'string') {
            const cmd = JSON.parse(event.data);
            
            if (cmd.type === 'set_mic_mute') {
               // MATCH THIS TO YOUR BUFFER_DELAY ABOVE
                const currentDelay = 0; 
                
                const targetVolume = cmd.mute ? 0 : 1;
                const scheduledTime = virtualMicCtx.currentTime + (cmd.mute ? 0 : currentDelay);

                // Use setValueAtTime so the volume flips EXACTLY when the audio starts
                gainNode.gain.setValueAtTime(targetVolume, scheduledTime);
                
                console.log(`[AudioPayload] ${cmd.mute ? 'Mute' : 'Unmute'} scheduled for ${scheduledTime}s`);
                return;
            }
            if (cmd.type === 'stop_audio') {
                nextPlayTime = 0; 
                console.log("[AudioPayload] AI Playback Interrupted.");
                return;
            }
        }
        if (event.data instanceof ArrayBuffer) {
            const int16Data = new Int16Array(event.data);
            const float32Data = new Float32Array(int16Data.length);
            for (let i = 0; i < int16Data.length; i++) {
                float32Data[i] = int16Data[i] / 32768.0;
            }

            if (virtualMicCtx.state !== 'running') {
                console.log(`[AudioPayload] AudioContext not running (state=${virtualMicCtx.state}) when audio arrived; forcing resume.`);
                await virtualMicCtx.resume();
            }

            const buffer = virtualMicCtx.createBuffer(1, float32Data.length, 24000);
            buffer.getChannelData(0).set(float32Data);

            const source = virtualMicCtx.createBufferSource();
            source.buffer = buffer;
            source.connect(gainNode);

            const now = virtualMicCtx.currentTime;
            const BUFFER_DELAY =0.5; // second buffer to give time to the classifier
            if (nextPlayTime < now) nextPlayTime = now + BUFFER_DELAY;
        
            source.start(nextPlayTime);
            nextPlayTime += buffer.duration;
        }
    };

    // CAPTURE: Meeting -> AI
    const captureCtx = new AudioContext({ sampleRate: 24000 });
    const captureDest = captureCtx.createMediaStreamDestination();
    const connectedStreamIds = new Set();
    let announcedParticipant = false;

    function connectMediaStream(stream) {
        if (!stream || connectedStreamIds.has(stream.id)) return;
        console.log(
        `[CAPTURE] Connected stream ${stream.id} from HTML element or srcObject interception`
    );
        
        // ECHO CANCELLATION: Don't capture our own AI voice
        // 1. Check if the track is our virtual mic track
        if (stream.getAudioTracks()[0] === window.__virtualMicTrack) return;

        // 2. NEW: Check if the stream object itself is our virtual destination stream
        if (window.__virtualMicCtx && stream === window.__virtualMicCtx.destination.stream) {
            console.log('[AudioPayload] Ignoring local destination stream to prevent loopback.');
            return;
        }

        try {
            const source = captureCtx.createMediaStreamSource(stream);
            source.connect(captureDest);
            connectedStreamIds.add(stream.id);
            console.log('[AudioPayload] Capturing teammate:', stream.id);
            // NOTE: do NOT announce "participant_joined" here. A stream connecting
            // just means someone's audio track is present in the call, which is
            // also true for the silent signed-in admitter bot while it's admitting
            // us. Announcing here caused the AI to greet an empty room before the
            // real candidate arrived. We instead announce on first actual detected
            // speech, in the VAD loop below, since the admitter never talks.
        } catch (e) { }
    }

    // Capture Loop with Auto-Resume
    async function startCaptureLoop() {

        if (captureCtx.state === 'suspended') await captureCtx.resume();

        const mixedTrack = captureDest.stream.getAudioTracks()[0];
        if (!mixedTrack) return setTimeout(startCaptureLoop, 1000);

        // 1. Setup Filters
        const hpFilter = captureCtx.createBiquadFilter();
        hpFilter.type = 'highpass';
        hpFilter.frequency.value = 250; // Removes low hum/thumps

        const bpFilter = captureCtx.createBiquadFilter();
        bpFilter.type = 'bandpass';
        bpFilter.frequency.value = 1850; // Focuses on human speech range
        bpFilter.Q.value = 1.0;

        const preAmp = captureCtx.createGain();
        preAmp.gain.value = 2.0; // Boost the "cleaned" voice for better STT

        // 2. Connect the "Cleaning Chain"
        const source = captureCtx.createMediaStreamSource(captureDest.stream);
        const filteredDest = captureCtx.createMediaStreamDestination();

        // Source -> HighPass -> BandPass -> PreAmp -> AI Reader
        source.connect(hpFilter);
        hpFilter.connect(bpFilter);
        bpFilter.connect(preAmp);
        preAmp.connect(filteredDest);

        // Use the FILTERED track for processing instead of the raw mixedTrack
        const finalTrack = filteredDest.stream.getAudioTracks()[0];
        const processor = new MediaStreamTrackProcessor({ track: finalTrack });
        const reader = processor.readable.getReader();

        let isSpeakingLocal = false;
        let silenceTimer = null;
        let consecutiveSpeechFrames = 0; // NEW: Counter to ignore short clicks/noise
        const SPEECH_CONFIRM_THRESHOLD = 5; // Must be loud for ~5 frames (approx 100ms) to count as speech
        const HANGOVER_MS = 1000; 

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            if (socket.readyState === WebSocket.OPEN) {
                const float32Data = new Float32Array(value.numberOfFrames);
                value.copyTo(float32Data, { planeIndex: 0 });

                const pcm16Data = new Int16Array(float32Data.length);
                for (let i = 0; i < float32Data.length; i++) {
                    const s = Math.max(-1, Math.min(1, float32Data[i]));
                    pcm16Data[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                }

                const rms = Math.sqrt(
                    float32Data.reduce((sum, val) => sum + val * val, 0) / float32Data.length
                );

                // --- 2. UPDATED SPEECH DETECTION LOGIC ---
                // Lowered RMS threshold slightly because the filter removes noise
                const isLoud = rms > 0.05; 

                if (isLoud) {
                    consecutiveSpeechFrames++;
                } else {
                    consecutiveSpeechFrames = 0;
                }

                // Only trigger "Speaking" if it's loud AND it's a sustained sound (not a click)
                const currentlyHearingVoice = consecutiveSpeechFrames >= SPEECH_CONFIRM_THRESHOLD;

                

                if (currentlyHearingVoice) {

                    //window.__virtualMicGainNode.gain.setValueAtTime(0, virtualMicCtx.currentTime);
                    if (!isSpeakingLocal) {
                        isSpeakingLocal = true;
                        socket.send(JSON.stringify({ type: "speaking_status", speaking: true }));
                    }
                    if (silenceTimer) {
                        clearTimeout(silenceTimer);
                        silenceTimer = null;
                    }
                } else {
                    if (isSpeakingLocal && !silenceTimer) {
                        silenceTimer = setTimeout(() => {
                            isSpeakingLocal = false;
                            socket.send(JSON.stringify({ type: "speaking_status", speaking: false }));
                            silenceTimer = null;
                            console.log("[VAD] Turn ended after 1.2s silence.");

                            // Greet only once real speech (never produced by the
                            // silent admitter bot) has happened AND finished, so
                            // the AI never talks over the candidate's first words.
                            if (!announcedParticipant) {
                                announcedParticipant = true;
                                socket.send(JSON.stringify({ type: 'participant_joined' }));
                                console.log('[AudioPayload] Real participant confirmed, notified backend to greet.');
                            }
                        }, HANGOVER_MS);
                    }
                }
                socket.send(pcm16Data.buffer);
            }
            value.close();
        }
    }

    // Deep scan for media elements including open shadow DOMs
    function findMediaElements(root, elements = []) {
        if (!root) return elements;
        if (root.tagName === 'AUDIO' || root.tagName === 'VIDEO') {
            elements.push(root);
        }
        if (root.shadowRoot) {
            findMediaElements(root.shadowRoot, elements);
        }
        if (root.children) {
            for (let i = 0; i < root.children.length; i++) {
                findMediaElements(root.children[i], elements);
            }
        }
        return elements;
    }

    // Intercept native srcObject assignment in case Webex uses closed shadow DOMs
    const originalSrcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
    if (originalSrcObject && originalSrcObject.set) {
        Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
            set: function(stream) {
                if (stream) {
                    // Try to connect the stream immediately when it's assigned
                    setTimeout(() => connectMediaStream(stream), 100);
                }
                return originalSrcObject.set.call(this, stream);
            },
            get: function() {
                return originalSrcObject.get.call(this);
            }
        });
        console.log('[AudioPayload] Intercepted HTMLMediaElement.prototype.srcObject');
    }

    // Initial scan and continuous scan for new participants targeting Shadow DOMs
    setInterval(() => {
        // Read raw streams hijacked by the Python interceptor script
        if (window.__inboundRTCStreams) {
            window.__inboundRTCStreams.forEach(stream => connectMediaStream(stream));
        }
        window.__onNewRTCStream = (stream) => {
            console.log('[AudioPayload] Captured new stream directly from WebRTC interceptor!');
            connectMediaStream(stream);
        };

        findMediaElements(document.body).forEach(el => {
            if (el.srcObject) connectMediaStream(el.srcObject);
        });
    }, 2000);

    startCaptureLoop();
})();