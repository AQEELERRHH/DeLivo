# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
# ============================================================
#  DeLivo — AI-Powered Parcel Delivery Verification
#  Built on GenLayer Intelligent Contracts
#  "Trust built into every handoff."
#
#  v4 — fixed genvm-lint types:
#       u256 for payment_amount, pickup_time, delivery_time
#       DynArray for waypoints
# ============================================================

from genlayer import *
import json


class DeLivo(gl.Contract):
    """
    DeLivo: end-to-end delivery verification.

    Lifecycle:
      pending → picked_up → in_transit → ai_reviewing
             → ai_approved / disputed
             → confirmed   / escalated
             → paid        / refunded
    """

    # ── Core identifiers ──────────────────────────────────────
    delivery_id:    str
    shipper:        str
    driver:         str
    recipient:      str
    payment_amount: u256        # ← sized type (was int)

    # ── Mode ──────────────────────────────────────────────────
    test_mode: bool

    # ── Status ────────────────────────────────────────────────
    status: str

    # ── Pickup telemetry ──────────────────────────────────────
    pickup_gps:  str
    pickup_time: u256           # ← sized type (was int)

    # ── Delivery telemetry ────────────────────────────────────
    delivery_gps:    str
    delivery_time:   u256       # ← sized type (was int)
    photo_proof_url: str

    # ── AI verification result ────────────────────────────────
    ai_fraud_risk: str
    ai_route_ok:   bool
    ai_photo_ok:   bool
    ai_reasoning:  str

    # ── Recipient confirmation ────────────────────────────────
    recipient_confirmed: bool
    dispute_reason:      str

    # ── Waypoints ─────────────────────────────────────────────
    waypoints: DynArray[str, 50]   # ← DynArray instead of list
    # Each entry is a JSON string: {"lat": float, "lng": float, "time": int, "note": str}

    # ═══════════════════════════════════════════════════════════
    #  CONSTRUCTOR
    # ═══════════════════════════════════════════════════════════

    def __init__(
        self,
        delivery_id: str = "",
        driver:      str = "",
        recipient:   str = "",
        test_mode:   bool = False,
    ):
        sender = gl.message.sender_address

        # Auto-generate delivery_id if blank
        if delivery_id.strip():
            self.delivery_id = delivery_id.strip()
        else:
            ts = gl.message.timestamp if hasattr(gl.message, "timestamp") else 0
            self.delivery_id = f"DLVR-{sender[-6:].upper()}-{ts}"

        self.shipper   = sender
        self.driver    = driver.strip()    or sender
        self.recipient = recipient.strip() or sender

        # Auto-enable test_mode when all roles default to deployer
        all_same = (self.driver == sender and self.recipient == sender)
        self.test_mode = test_mode or all_same

        self.payment_amount = gl.message.value
        self.status         = "pending"

        self.pickup_gps      = ""
        self.pickup_time     = u256(0)
        self.delivery_gps    = ""
        self.delivery_time   = u256(0)
        self.photo_proof_url = ""

        self.ai_fraud_risk = ""
        self.ai_route_ok   = False
        self.ai_photo_ok   = False
        self.ai_reasoning  = ""

        self.recipient_confirmed = False
        self.dispute_reason      = ""
        self.waypoints           = DynArray[str, 50]([])

    # ═══════════════════════════════════════════════════════════
    #  INTERNAL: ROLE CHECK
    # ═══════════════════════════════════════════════════════════

    def _assert_role(self, expected_address: str, role_name: str):
        if not self.test_mode:
            assert gl.message.sender_address == expected_address, \
                f"Only the {role_name} can call this method"

    # ═══════════════════════════════════════════════════════════
    #  DRIVER ACTIONS
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def log_pickup(self, lat: float, lng: float, timestamp: u256):
        """Driver logs GPS + timestamp at moment of pickup."""
        self._assert_role(self.driver, "driver")
        assert self.status == "pending", \
            f"Cannot pick up — current status: {self.status}"

        self.pickup_gps  = json.dumps({"lat": lat, "lng": lng})
        self.pickup_time = timestamp
        self.status      = "picked_up"

    @gl.public.write
    def log_waypoint(self, lat: float, lng: float, timestamp: u256, note: str = ""):
        """Optional: add intermediate GPS checkpoint for audit trail."""
        self._assert_role(self.driver, "driver")
        assert self.status in ("picked_up", "in_transit"), \
            f"Cannot add waypoint — current status: {self.status}"

        waypoint = json.dumps({
            "lat":  lat,
            "lng":  lng,
            "time": int(timestamp),
            "note": note,
        })
        self.waypoints.append(waypoint)
        self.status = "in_transit"

    @gl.public.write
    def log_delivery(
        self,
        lat:       float,
        lng:       float,
        timestamp: u256,
        photo_url: str,
    ):
        """
        Driver submits delivery GPS + photo proof URL.
        Triggers DeLivo Shield AI verification via GenLayer validators.
        Validators screenshot the photo URL and analyse it visually.
        """
        self._assert_role(self.driver, "driver")
        assert self.status in ("picked_up", "in_transit"), \
            f"Cannot log delivery — current status: {self.status}"

        self.delivery_gps    = json.dumps({"lat": lat, "lng": lng})
        self.delivery_time   = timestamp
        self.photo_proof_url = photo_url
        self.status          = "ai_reviewing"

        self._run_ai_verification()

    # ═══════════════════════════════════════════════════════════
    #  AI VERIFICATION (non-deterministic — GenLayer validators)
    # ═══════════════════════════════════════════════════════════

    def _run_ai_verification(self):
        pickup_data   = json.loads(self.pickup_gps)
        delivery_data = json.loads(self.delivery_gps)

        # Parse waypoints from DynArray of JSON strings
        parsed_waypoints = []
        for wp_str in self.waypoints:
            try:
                parsed_waypoints.append(json.loads(wp_str))
            except Exception:
                pass

        context = {
            "delivery_id":   self.delivery_id,
            "pickup_gps":    pickup_data,
            "delivery_gps":  delivery_data,
            "pickup_time":   int(self.pickup_time),
            "delivery_time": int(self.delivery_time),
            "waypoints":     parsed_waypoints,
            "photo_url":     self.photo_proof_url,
            "test_mode":     self.test_mode,
        }

        def verify_delivery():
            images = []
            if self.photo_proof_url.startswith("http"):
                try:
                    screenshot = gl.nondet.web.render(
                        self.photo_proof_url,
                        mode="screenshot"
                    )
                    images = [screenshot]
                except Exception:
                    pass

            test_note = (
                "\n\nNOTE: TEST MODE — be lenient, focus on route plausibility, "
                "accept missing photo proof."
                if self.test_mode else ""
            )

            result = gl.nondet.exec_prompt(
                f"""You are DeLivo Shield — an AI fraud detection engine for parcel delivery.

Analyse this delivery and return ONLY valid JSON. No markdown, no preamble.

Delivery data:
{json.dumps(context, indent=2)}
{test_note}

{"Photo attached for visual analysis." if images else "No photo retrieved."}

Evaluate:
1. ROUTE CONSISTENCY   — Plausible GPS path? Reasonable transit time?
2. ANOMALY DETECTION   — GPS spoofing? Impossible speed? Suspicious stops?
3. PHOTO PROOF         — Real delivery scene? Package visible?
4. FRAUD RISK          — "low", "medium", or "high"

Return EXACTLY:
{{
  "fraud_risk": "low|medium|high",
  "route_ok":   true|false,
  "photo_ok":   true|false,
  "reasoning":  "Max 200 words"
}}""",
                images=images if images else None,
            )

            clean = result.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            return json.loads(clean.strip())

        verification = gl.eq_principle_prompt_non_comparative(
            verify_delivery,
            "Validators must agree on fraud_risk (low/medium/high), "
            "route_ok (bool), and photo_ok (bool)."
        )

        self.ai_fraud_risk = verification.get("fraud_risk", "high")
        self.ai_route_ok   = bool(verification.get("route_ok", False))
        self.ai_photo_ok   = bool(verification.get("photo_ok", False))
        self.ai_reasoning  = verification.get("reasoning", "")

        if (
            self.ai_fraud_risk == "low"
            and self.ai_route_ok
            and self.ai_photo_ok
        ):
            self.status = "ai_approved"
        else:
            self.status = "disputed"

    # ═══════════════════════════════════════════════════════════
    #  TEST HELPER
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def run_test_delivery(self):
        """
        ONE-CLICK TEST — simulates a Lagos → Ikeja delivery.
        Only available in test_mode (auto-enabled on blank deploys).
        """
        assert self.test_mode, "run_test_delivery() is only available in test_mode."
        assert self.status == "pending", \
            f"Status is '{self.status}' — deploy a fresh contract to test again."

        t0 = u256(1_748_000_000)
        t1 = u256(1_748_001_200)
        t2 = u256(1_748_002_700)

        # Pickup — Lagos Island
        self.pickup_gps  = json.dumps({"lat": 6.4550, "lng": 3.3841})
        self.pickup_time = t0
        self.status      = "picked_up"

        # Waypoint — Third Mainland Bridge
        self.waypoints.append(json.dumps({
            "lat": 6.4698, "lng": 3.3887,
            "time": int(t1),
            "note": "Third Mainland Bridge checkpoint",
        }))
        self.status = "in_transit"

        # Delivery — Ikeja GRA
        self.delivery_gps    = json.dumps({"lat": 6.6018, "lng": 3.3515})
        self.delivery_time   = t2
        self.photo_proof_url = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/"
            "4/47/PNG_transparency_demonstration_1.png/"
            "280px-PNG_transparency_demonstration_1.png"
        )
        self.status = "ai_reviewing"

        self._run_ai_verification()

    # ═══════════════════════════════════════════════════════════
    #  RECIPIENT ACTIONS
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def confirm_delivery(self):
        """Recipient confirms receipt → releases payment to driver."""
        self._assert_role(self.recipient, "recipient")
        assert self.status in ("ai_approved", "disputed"), \
            f"Cannot confirm — current status: {self.status}"

        self.recipient_confirmed = True
        self.status = "confirmed"
        gl.transfer(self.driver, self.payment_amount)

    @gl.public.write
    def raise_dispute(self, reason: str):
        """Escalates to GenLayer's Optimistic Democracy validator court."""
        self._assert_role(self.recipient, "recipient")
        assert self.status in ("ai_approved", "disputed"), \
            f"Cannot raise dispute — current status: {self.status}"

        self.dispute_reason = reason
        self.status = "escalated"

    @gl.public.write
    def resolve_dispute(self, favour_driver: bool):
        """Finalise escalated dispute. favour_driver=True pays driver, False refunds shipper."""
        self._assert_role(self.shipper, "shipper")
        assert self.status == "escalated", \
            f"No active dispute — current status: {self.status}"

        if favour_driver:
            gl.transfer(self.driver, self.payment_amount)
            self.status = "paid"
        else:
            gl.transfer(self.shipper, self.payment_amount)
            self.status = "refunded"

    @gl.public.write
    def cancel_delivery(self):
        """Cancel before pickup and reclaim escrowed payment."""
        self._assert_role(self.shipper, "shipper")
        assert self.status == "pending", "Can only cancel before pickup"
        gl.transfer(self.shipper, self.payment_amount)
        self.status = "cancelled"

    # ═══════════════════════════════════════════════════════════
    #  READ-ONLY VIEWS
    # ═══════════════════════════════════════════════════════════

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "delivery_id": self.delivery_id,
            "shipper":     self.shipper,
            "driver":      self.driver,
            "recipient":   self.recipient,
            "test_mode":   self.test_mode,
            "tip": (
                "test_mode ON — call run_test_delivery() to test the full flow"
                if self.test_mode
                else "Production mode — role checks are strict"
            ),
        }

    @gl.public.view
    def get_delivery_summary(self) -> dict:
        return {
            "delivery_id":     self.delivery_id,
            "status":          self.status,
            "driver":          self.driver,
            "recipient":       self.recipient,
            "payment_amount":  int(self.payment_amount),
            "pickup_time":     int(self.pickup_time),
            "delivery_time":   int(self.delivery_time),
            "photo_proof_url": self.photo_proof_url,
            "waypoint_count":  len(self.waypoints),
        }

    @gl.public.view
    def get_ai_verdict(self) -> dict:
        return {
            "fraud_risk": self.ai_fraud_risk,
            "route_ok":   self.ai_route_ok,
            "photo_ok":   self.ai_photo_ok,
            "reasoning":  self.ai_reasoning,
        }

    @gl.public.view
    def get_status(self) -> str:
        return self.status

    @gl.public.view
    def is_paid(self) -> bool:
        return self.status in ("confirmed", "paid")
