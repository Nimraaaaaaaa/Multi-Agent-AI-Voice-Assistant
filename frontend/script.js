(() => {
    "use strict";

    const hudClock = document.getElementById("hudClock");
    const commandForm = document.getElementById("commandForm");
    const commandInput = document.getElementById("commandInput");
    const terminalLog = document.getElementById("terminalLog");
    const statusTitle = document.getElementById("statusTitle");
    const globeContainer = document.getElementById("globeCanvas");
    const starField = document.getElementById("starField");
    const coreWrapper = document.getElementById("coreWrapper");


    /* =====================================================
       LIVE CLOCK
       ===================================================== */

    function updateClock() {
        const now = new Date();

        hudClock.textContent = now.toLocaleTimeString("en-GB", {
            hour12: false
        });
    }

    setInterval(updateClock, 1000);
    updateClock();


    /* =====================================================
       TERMINAL LOGGER

       Newest message is always placed at the TOP.
       ===================================================== */

    function addLog(msg) {
        const entry = document.createElement("div");

        entry.className = "log-entry";

        entry.innerHTML =
            `<span class="tag">&rsaquo;&rsaquo;</span> <span>${msg}</span>`;

        // NEWEST MESSAGE → TOP
        terminalLog.prepend(entry);

        // Always keep newest message visible
        terminalLog.scrollTop = 0;
    }

    addLog("J.A.R.V.I.S. system initialized. Sci-Fi HUD connected.");


    /* =====================================================
       BACKGROUND NEON STAR / DOT FIELD
       ===================================================== */

    function buildStarField(count = 90) {

        if (!starField) return;

        const frag = document.createDocumentFragment();

        for (let i = 0; i < count; i++) {

            const dot = document.createElement("span");

            dot.className = "dot";

            const size = Math.random() * 2 + 1;

            dot.style.width = `${size}px`;
            dot.style.height = `${size}px`;

            dot.style.left = `${Math.random() * 100}%`;
            dot.style.top = `${Math.random() * 100}%`;

            dot.style.animationDelay =
                `${Math.random() * 3.5}s`;

            frag.appendChild(dot);
        }

        starField.appendChild(frag);
    }

    buildStarField();


    /* =====================================================
       3D WIREFRAME SPHERE CORE

       Existing sphere behavior preserved.
       ===================================================== */

    let scene;
    let camera;
    let renderer;
    let wireMesh;
    let particlesMesh;
    let innerGlow;

    const CORE_COLOR = 0x00d8ff;


    function init3D() {

        const width =
            globeContainer.clientWidth || 300;

        const height =
            globeContainer.clientHeight || 300;


        scene = new THREE.Scene();


        camera = new THREE.PerspectiveCamera(
            45,
            width / height,
            0.1,
            100
        );

        camera.position.z = 4.8;


        renderer = new THREE.WebGLRenderer({
            antialias: true,
            alpha: true
        });

        renderer.setSize(width, height);

        renderer.setPixelRatio(
            Math.min(window.devicePixelRatio, 2)
        );

        globeContainer.appendChild(renderer.domElement);


        /* Wireframe sphere */

        const geo =
            new THREE.IcosahedronGeometry(1.4, 2);


        const wireMat =
            new THREE.MeshBasicMaterial({
                color: CORE_COLOR,
                wireframe: true,
                transparent: true,
                opacity: 0.35
            });


        wireMesh =
            new THREE.Mesh(geo, wireMat);

        scene.add(wireMesh);


        /* Sphere particles */

        const ptGeo =
            new THREE.IcosahedronGeometry(1.4, 3);


        const ptMat =
            new THREE.PointsMaterial({
                color: 0x00ffff,
                size: 0.04,
                transparent: true,
                opacity: 0.85,
                blending: THREE.AdditiveBlending
            });


        particlesMesh =
            new THREE.Points(ptGeo, ptMat);

        scene.add(particlesMesh);


        /*
        const innerLightGeo =
            new THREE.SphereGeometry(0.3, 16, 16);

        const innerLightMat =
            new THREE.MeshBasicMaterial({
                color: CORE_COLOR,
                transparent: true,
                opacity: 0.6
            });

        innerGlow =
            new THREE.Mesh(
                innerLightGeo,
                innerLightMat
            );

        scene.add(innerGlow);
        */


        animate();
    }


    /* =====================================================
       ROTATION SPEED

       Busy = fast
       Listening = normal
       Idle = normal
       ===================================================== */

    let rotSpeed = 1;
    let rotTarget = 1;


    function animate() {

        requestAnimationFrame(animate);


        rotSpeed +=
            (rotTarget - rotSpeed) * 0.05;


        wireMesh.rotation.y +=
            0.003 * rotSpeed;

        wireMesh.rotation.x +=
            0.001 * rotSpeed;


        particlesMesh.rotation.y -=
            0.004 * rotSpeed;

        particlesMesh.rotation.z +=
            0.002 * rotSpeed;


        renderer.render(scene, camera);
    }


    init3D();


    /* =====================================================
       STATE MANAGER

       IMPORTANT:

       busy      = existing execution animation
       listening = listening heartbeat only
       idle      = normal state

       STT itself is NOT controlled here.
       ===================================================== */

    function setSystemState(state, customText) {

        if (state === "busy") {

            document.body.setAttribute(
                "data-state",
                "busy"
            );

            statusTitle.textContent =
                customText ||
                "PROCESSING COMMAND...";


            // EXISTING EXECUTION SPEED
            rotTarget = 4.5;


        } else if (state === "listening") {

            document.body.setAttribute(
                "data-state",
                "listening"
            );

            statusTitle.textContent =
                customText ||
                "LISTENING...";


            // Keep normal sphere rotation.
            // CSS handles the heartbeat.
            rotTarget = 1;


        } else {

            document.body.setAttribute(
                "data-state",
                "idle"
            );

            statusTitle.textContent =
                "ENTER YOUR COMMAND";


            rotTarget = 1;
        }
    }


    /* =====================================================
       BUSY FALLBACK TIMER
       ===================================================== */

    let busyFallbackTimer = null;


    function goBusy(text) {

        setSystemState(
            "busy",
            text
        );


        if (busyFallbackTimer) {
            clearTimeout(busyFallbackTimer);
        }


        busyFallbackTimer =
            setTimeout(() => {

                setSystemState("idle");

            }, 15000);
    }


    /* =====================================================
       LISTENING STATE

       Only changes visual state.
       Does NOT touch microphone / STT.
       ===================================================== */

    function goListening() {

        if (busyFallbackTimer) {

            clearTimeout(
                busyFallbackTimer
            );

            busyFallbackTimer = null;
        }


        setSystemState(
            "listening",
            "LISTENING..."
        );
    }


    /* =====================================================
       IDLE STATE
       ===================================================== */

    function goIdle() {

        if (busyFallbackTimer) {

            clearTimeout(
                busyFallbackTimer
            );

            busyFallbackTimer = null;
        }


        setSystemState("idle");
    }


    /* =====================================================
       REAL BACKEND CONNECTION
       WEBSOCKET
       ===================================================== */

    let socket;


    function connectSocket() {

        const wsUrl =
            `ws://${window.location.host}/ws`;


        socket =
            new WebSocket(wsUrl);


        /* -------------------------------------------------
           CONNECTED
           ------------------------------------------------- */

        socket.onopen = () => {

            addLog(
                "Connected to JARVIS backend."
            );
        };


        /* -------------------------------------------------
           DISCONNECTED
           ------------------------------------------------- */

        socket.onclose = () => {

            addLog(
                "Disconnected from backend. Retrying in 3s..."
            );


            setTimeout(
                connectSocket,
                3000
            );
        };


        /* -------------------------------------------------
           ERROR
           ------------------------------------------------- */

        socket.onerror = (err) => {

            console.error(
                "WebSocket error:",
                err
            );
        };


        /* -------------------------------------------------
           INCOMING BACKEND EVENTS
           ------------------------------------------------- */

        socket.onmessage = (event) => {

            const data =
                JSON.parse(event.data);


            /* =============================================
               STATUS EVENTS
               ============================================= */

            if (data.event === "status") {

                const state =
                    String(
                        data.state || ""
                    ).toLowerCase();


                console.log(
                    "JARVIS STATE:",
                    state
                );


                /* -----------------------------------------
                   LISTENING

                   → heartbeat ON
                   → sphere normal speed
                   ----------------------------------------- */

                if (state === "listening") {

                    goListening();
                }


                /* -----------------------------------------
                   THINKING / PROCESSING

                   → EXISTING execution animation
                   ----------------------------------------- */

                else if (
                    state === "thinking" ||
                    state === "processing"
                ) {

                    goBusy(
                        "PROCESSING..."
                    );
                }


                /* -----------------------------------------
                   IDLE

                   → normal interface
                   ----------------------------------------- */

                else if (
                    state === "idle" ||
                    state === "ready"
                ) {

                    goIdle();
                }

                /*
                   IMPORTANT:

                   Unknown states are ignored.

                   We DON'T automatically call goIdle()
                   because some backend state shouldn't
                   interrupt the current visual state.
                */
            }


            /* =============================================
               USER VOICE TRANSCRIPT
               ============================================= */

            if (
                data.event ===
                "user_transcript"
            ) {

                addLog(
                    `COMMAND (voice): ${data.text}`
                );


                /*
                   Speech has been detected.
                   Listening heartbeat ends.
                   Existing processing animation starts.
                */

                goBusy(
                    "PROCESSING COMMAND..."
                );
            }


            /* =============================================
               JARVIS RESPONSE
               ============================================= */

            if (
                data.event ===
                "jarvis_reply"
            ) {

                addLog(
                    `RESPONSE: ${data.text}`
                );


                goIdle();
            }
        };
    }


    connectSocket();


    /* =====================================================
       COMMAND FORM HANDLER
       ===================================================== */

    commandForm.addEventListener(
        "submit",
        (e) => {

            e.preventDefault();


            const val =
                commandInput.value.trim();


            if (!val) return;


            /* Add command to TOP */

            addLog(
                `COMMAND: ${val}`
            );


            commandInput.value = "";


            /* Check backend connection */

            if (
                !socket ||
                socket.readyState !==
                WebSocket.OPEN
            ) {

                addLog(
                    "Not connected to backend yet. Try again in a moment."
                );

                return;
            }


            /* Existing execution animation */

            goBusy(
                "PROCESSING COMMAND..."
            );


            /* Send command */

            socket.send(
                JSON.stringify({
                    event: "user_message",
                    text: val
                })
            );
        }
    );

})();