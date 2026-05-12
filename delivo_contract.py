# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
# ============================================================
#  DeLivo AI-Powered Parcel Delivery Verification
#  Built on GenLayer Intelligent Contracts
#  "Trust built into every handoff."
#
# ============================================================
#
#  QUICK START (blank deploy on Shipyard):
#
#  1. Deploy with all fields empty → test_mode auto-enables
#  2. Call get_config()            → confirm addresses + ID
#  3. Call run_test_delivery()     → runs full happy-path flow
#     with realistic sample data, no real photo URL needed
#  4. Call get_ai_verdict()        → see the AI reasoning
#  5. Call confirm_delivery()      → release payment, done ✓
#
#  PRODUCTION deploy:
#    delivery_id = "DLVR-001"
#    driver      = 0xDriverWalletAddress
#    recipient   = 0xRecipientWalletAddress
#    test_mode   = False  (default)
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
    shipper:        str     # Deployer — holds escrow, resolves disputes
    driver:         str     # Authorised to log pickup + delivery
    recipient:      str     # Authorised to confirm or dispute
    payment_amount: int     # Escrowed at deploy time

    # ── Mode ──────────────────────────────────────────────────
    test_mode: bool         # True → relaxed role checks + mock AI fallback

    # ── Delivery status ───────────────────────────────────────
    status: str

    # ── Pickup telemetry ──────────────────────────────────────
    pickup_gps:  str        # JSON {"lat": float, "lng": float}
    pickup_time: int        # Unix timestamp

    # ── Delivery telemetry ────────────────────────────────────
    delivery_gps:    str
    delivery_time:   int
    photo_proof_url: str    # IPFS / Arweave / HTTPS URL

    # ── AI verification result ────────────────────────────────
    ai_fraud_risk: str      # "low" | "medium" | "high"
    ai_route_ok:   bool
    ai_photo_ok:   bool
    ai_reasoning:  str

    # ── Recipient confirmation ────────────────────────────────
    recipient_confirmed: bool
    dispute_reason:      str

    # ── Waypoints (optional audit trail) ─────────────────────
    waypoints: list

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

        # ── delivery_id: auto-generate if blank ───────────────
        if delivery_id.strip():
            self.delivery_id = delivery_id.strip()
        else:
            ts = gl.message.timestamp if hasattr(gl.message, "timestamp") else 0
            self.delivery_id = f"DLVR-{sender[-6:].upper()}-{ts}"

        self.shipper = sender

        # ── driver / recipient: default to deployer if blank ──
        self.driver    = driver.strip()    or sender
        self.recipient = recipient.strip() or sender

        # ── test_mode: auto-enable on blank deploy ─────────────
        # When all three roles point to the same address it means
        # someone deployed without filling in args — enable test_mode
        # automatically so they can run the full flow without errors.
        all_same = (self.driver == sender and self.recipient == sender)
        self.test_mode = test_mode or all_same

        self.payment_amount = gl.message.value
        self.status         = "pending"

        self.pickup_gps      = ""
        self.pickup_time     = 0
        self.delivery_gps    = ""
        self.delivery_time   = 0
        self.photo_proof_url = ""

        self.ai_fraud_risk = ""
        self.ai_route_ok   = False
        self.ai_photo_ok   = False
        self.ai_reasoning  = ""

        self.recipient_confirmed = False
        self.dispute_reason      = ""
        self.waypoints           = []

    # ═══════════════════════════════════════════════════════════
    #  INTERNAL: ROLE CHECK (relaxed in test_mode)
    # ═══════════════════════════════════════════════════════════

    def _assert_role(self, expected_address: str, role_name: str):
        """
        In test_mode any caller can act in any role.
        In production mode the address check is strict.
        """
        if not self.test_mode:
            assert gl.message.sender_address == expected_address, \
                f"Only the {role_name} can call this method"

    # ═══════════════════════════════════════════════════════════
    #  DRIVER ACTIONS
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def log_pickup(self, lat: float, lng: float, timestamp: int):
        """Driver logs GPS + timestamp at moment of pickup."""
        self._assert_role(self.driver, "driver")
        assert self.status == "pending", \
            f"Cannot pick up — current status: {self.status}"

        self.pickup_gps  = json.dumps({"lat": lat, "lng": lng})
        self.pickup_time = timestamp
        self.status      = "picked_up"

    @gl.public.write
    def log_waypoint(self, lat: float, lng: float, timestamp: int, note: str = ""):
        """Optional: add intermediate GPS checkpoint for audit trail."""
        self._assert_role(self.driver, "driver")
        assert self.status in ("picked_up", "in_transit"), \
            f"Cannot add waypoint — current status: {self.status}"

        self.waypoints.append({
            "gps":  {"lat": lat, "lng": lng},
            "time": timestamp,
            "note": note,
        })
        self.status = "in_transit"

    @gl.public.write
    def log_delivery(
        self,
        lat:       float,
        lng:       float,
        timestamp: int,
        photo_url: str,
    ):
        """
        Driver submits delivery GPS + photo proof URL.
        Triggers DeLivo Shield AI verification via GenLayer validators.

        photo_url: IPFS / Arweave / HTTPS URL to delivery photo.
                   In test_mode you can pass any string — AI will still
                   run but note the photo could not be fetched.
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

        context = {
            "delivery_id":   self.delivery_id,
            "pickup_gps":    pickup_data,
            "delivery_gps":  delivery_data,
            "pickup_time":   self.pickup_time,
            "delivery_time": self.delivery_time,
            "waypoints":     self.waypoints,
            "photo_url":     self.photo_proof_url,
            "test_mode":     self.test_mode,
        }

        def verify_delivery():
            # Fetch photo — gracefully handle missing / invalid URLs
            photo_content = "No photo URL provided."
            if self.photo_proof_url.startswith("http"):
                try:
                    photo_content = gl.get_webpage(
                        self.photo_proof_url, mode="text"
                    )[:2000]
                except Exception as e:
                    photo_content = f"Photo fetch failed: {e}"

            test_note = (
                "\n\nNOTE: This is a TEST MODE delivery. "
                "Be lenient — treat an unverifiable or missing photo "
                "as acceptable. Evaluate route plausibility only."
                if self.test_mode else ""
            )

            result = gl.exec_prompt(f"""
You are DeLivo Shield — an AI fraud detection engine for parcel delivery.

Analyse this delivery and return ONLY a valid JSON object.
No markdown, no preamble, no text outside the JSON.

Delivery data:
{json.dumps(context, indent=2)}

Photo evidence:
{photo_content}
{test_note}

Evaluate:
1. ROUTE CONSISTENCY   — Is delivery GPS plausible from pickup? Is transit time reasonable?
2. ANOMALY DETECTION   — GPS spoofing? Impossible speed? Unexplained long stops? Reroutes?
3. PHOTO PROOF         — Does evidence suggest a real delivery? (lenient if test_mode=true)
4. FRAUD RISK          — "low", "medium", or "high"

Return EXACTLY this JSON:
{{
  "fraud_risk": "low|medium|high",
  "route_ok":   true|false,
  "photo_ok":   true|false,
  "reasoning":  "Max 200 words"
}}
""")
            clean = result.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            return json.loads(clean.strip())

        verification = gl.eq_principle_prompt_non_comparative(
            verify_delivery,
            "Validators must agree on fraud_risk (low/medium/high), "
            "route_ok (bool), and photo_ok (bool). Reasoning may differ."
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
    #  ⚡ TEST HELPER — full happy-path in one call
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def run_test_delivery(self):
        """
        ONE-CLICK TEST FLOW — simulates a complete Lagos → Ikeja delivery.

        Runs automatically:
          log_pickup → log_waypoint → log_delivery → AI verify

        After this returns, call:
          get_ai_verdict()    → see what DeLivo Shield decided
          confirm_delivery()  → complete the flow + release payment

        Only available in test_mode (auto-enabled on blank deploys).
        Deploy a fresh contract to run it again.
        """
        assert self.test_mode, \
            "run_test_delivery() is only available in test_mode. " \
            "Deploy with blank args to enable test_mode automatically."
        assert self.status == "pending", \
            f"Cannot run test — status is already '{self.status}'. " \
            "Deploy a fresh contract to test again."

        # ── Simulated Lagos → Ikeja delivery ─────────────────
        # Pickup:   Lagos Island (6.4550, 3.3841)
        # Waypoint: Third Mainland Bridge (6.4698, 3.3887)
        # Delivery: Ikeja GRA (6.6018, 3.3515)
        # Transit:  45 minutes — plausible for Lagos traffic

        t0 = 1_748_000_000          # base Unix timestamp
        t1 = t0 + 1_200             # +20 min  (waypoint)
        t2 = t0 + 2_700             # +45 min  (delivery)

        # Step 1 — Pickup
        self.pickup_gps  = json.dumps({"lat": 6.4550, "lng": 3.3841})
        self.pickup_time = t0
        self.status      = "picked_up"

        # Step 2 — Mid-route waypoint
        self.waypoints.append({
            "gps":  {"lat": 6.4698, "lng": 3.3887},
            "time": t1,
            "note": "Third Mainland Bridge checkpoint",
        })
        self.status = "in_transit"

        # Step 3 — Delivery with a real fetchable image as photo proof
        self.delivery_gps    = json.dumps({"lat": 6.6018, "lng": 3.3515})
        self.delivery_time   = t2
        self.photo_proof_url = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/"
            "4/47/PNG_transparency_demonstration_1.png/"
            "280px-PNG_transparency_demonstration_1.png"
        )
        self.status = "ai_reviewing"

        # Step 4 — AI verification (live GenLayer LLM consensus)
        self._run_ai_verification()

    # ═══════════════════════════════════════════════════════════
    #  RECIPIENT ACTIONS
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def confirm_delivery(self):
        """
        Recipient confirms receipt → releases payment to driver.
        Also works when AI flagged a dispute (recipient overrides AI).
        """
        self._assert_role(self.recipient, "recipient")
        assert self.status in ("ai_approved", "disputed"), \
            f"Cannot confirm — current status: {self.status}"

        self.recipient_confirmed = True
        self.status = "confirmed"
        gl.transfer(self.driver, self.payment_amount)

    @gl.public.write
    def raise_dispute(self, reason: str):
        """
        Recipient formally disputes the delivery.
        Escalates to GenLayer's Optimistic Democracy validator court.
        Payment stays locked in escrow until resolve_dispute() is called.
        """
        self._assert_role(self.recipient, "recipient")
        assert self.status in ("ai_approved", "disputed"), \
            f"Cannot raise dispute — current status: {self.status}"

        self.dispute_reason = reason
        self.status = "escalated"

    # ═══════════════════════════════════════════════════════════
    #  DISPUTE RESOLUTION
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def resolve_dispute(self, favour_driver: bool):
        """
        Finalise an escalated dispute.
        Production: called by GenLayer validator consensus network.
        Testnet:    shipper calls manually to simulate resolution.

        favour_driver=True  → pay driver, delivery considered valid
        favour_driver=False → refund shipper, delivery considered failed
        """
        self._assert_role(self.shipper, "shipper")
        assert self.status == "escalated", \
            f"No active dispute — current status: {self.status}"

        if favour_driver:
            gl.transfer(self.driver, self.payment_amount)
            self.status = "paid"
        else:
            gl.transfer(self.shipper, self.payment_amount)
            self.status = "refunded"

    # ═══════════════════════════════════════════════════════════
    #  SHIPPER CANCEL (before pickup only)
    # ═══════════════════════════════════════════════════════════

    @gl.public.write
    def cancel_delivery(self):
        """Cancel before pickup and reclaim escrowed payment."""
        self._assert_role(self.shipper, "shipper")
        assert self.status == "pending", \
            "Can only cancel before the driver picks up"

        gl.transfer(self.shipper, self.payment_amount)
        self.status = "cancelled"

    # ═══════════════════════════════════════════════════════════
    #  READ-ONLY VIEWS
    # ═══════════════════════════════════════════════════════════

    @gl.public.view
    def get_config(self) -> dict:
        """
        Call right after deploying to confirm what was set.
        Shows whether test_mode is active and what addresses were assigned.
        """
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
            "payment_amount":  self.payment_amount,
            "pickup_time":     self.pickup_time,
            "delivery_time":   self.delivery_time,
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
