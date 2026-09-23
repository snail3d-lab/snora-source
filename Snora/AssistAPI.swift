import Foundation

/// one planned render from the assist brain
struct AssistJob: Codable {
    let kind: String            // video | photo_qwen | qwen_edit | qwen_multi | qwen_sticker | photo_flux | img2img
    let prompt: String
    var quality: String?
    var aspect: String?
    var duration: Double?       // lenient: some models emit 5.0 instead of 5
    var ref_mode: String?
    var strength: Double?
    var use_form_photos: Bool?
    var use_form_voice: Bool?
    var transparent: Bool?      // qwen_sticker / cut-outs
}

/// the strict-JSON answer from the planner
struct AssistPlan: Codable {
    let message: String
    let jobs: [AssistJob]
}

/// the user's own snail-api models — the assist brain is NEVER an external API.
/// base/key read from your sora_config.json (2026-09-09) and baked.
enum AssistAPI {
    static let base = "http://127.0.0.1:8900/v1"
    static let key = "REPLACE_WITH_YOUR_SNAIL_API_KEY"

    /// the app's own mic log, readable with no login — the user pastes this URL to me
    /// and I can see exactly where a take died. The gateway's /v1 prefix does not
    /// route it, so it goes through the normal /api/sora path.
    static let logUrl = "https://YOUR-BACKEND.example.com/api/sora/assist/log"

    /// planner model — configurable in case the fleet swaps slots (default "snail-top",
    /// which today resolves to qwen3.8-flash-next on the gateway)
    static var model: String {
        let m = UserDefaults.standard.string(forKey: "assist_model") ?? ""
        return m.isEmpty ? "snail-top" : m
    }

    /// exact planner contract — the rules that keep the user's instructions authoritative.
    /// Kept line-by-line so each rule survives model attention.
    static let systemPrompt = """
    You are sNora's render assistant on the user's phone. He speaks casually and briefly.
    Return STRICT JSON only — no prose, no markdown:
    {"message": "short confirmation", "jobs": [ ... ]}
    Each job: {"kind": "video"|"photo_qwen"|"qwen_edit"|"qwen_multi"|"qwen_sticker"|"photo_flux"|"img2img", "prompt": "...", "quality": "fast|standard|best"?, "aspect": "wide|portrait|square"?, "duration": 3|5|10|15?, "ref_mode": "scene|identity"?, "strength": 0..1?, "use_form_photos": bool?, "use_form_voice": bool?, "transparent": bool?}
    3s is one quick line of action or a single short sentence spoken; 5s is the normal beat.

    RULES:
    1. CHARACTER LOYALTY: If the user names a character (Grandpa, Buddy, a robot, whoever),
       every job involving that character MUST keep the same name and description in its
       prompt. Never drop, rename, or swap a character he asked for. If a reference photo
       of that character is attached, set ref_mode so the photo is honored.
     2. HIS WORDS WIN: explicit settings in the transcript override the form. Anything he
       didn't mention inherits from `current`.
    3. COUNT: "three clips" means exactly 3 video jobs. Queue every item he lists.
    4. Videos get a vivid expanded prompt ending with a soundscape line. Photo prompts
       stay one rich sentence, no soundscape.
    5. PHOTO ENGINES — pick by what the ask needs:
       • qwen_edit — change something IN an attached photo while keeping the person,
         object, logo and text exactly as they are. Requires ≥1 photo (use_form_photos true).
       • qwen_multi — build one NEW picture from 2+ attached photos (put X with Y, swap an
         outfit, join people together). Requires ≥2 photos.
       • qwen_sticker — a cut-out sticker on a genuinely transparent background
         (set transparent true); works from words alone or from attached photos.
       • photo_qwen — any other picture from the Qwen engine, especially when it must
         contain real readable text or keep a face from a photo.
       • photo_flux — a plain artistic photo with no reference photo and no text.
       • img2img — Flux restyle of one photo (set strength).
       Qwen is the identity/text/transparency engine; Flux is the pretty-picture engine.
    6. If his ask is ambiguous, choose the most literal reading — never invent extras.
    7. DIALOGUE DISCIPLINE for every spoken line: budget ≈ 2.2 words per second of
       duration (3s≤7, 5s≤11, 8s≤18, 10s≤22, 15s≤33 words) — the farm rejects an
       over-budget line, so rewrite it shorter with meaning and voice intact; no
       filler (hey, hey y'all, y'all, just, literally, like, you know, I mean, uh, um,
       basically, actually, guys, okay so, a leading "so" or "well"); NEVER narrate
       what the shot already shows — the line adds a want, a joke, a name, or a
       memory; one intent, one speaker; write it as He says, <emotion>: "line" at the
       END of the video prompt; when the scene invites words and the user gave none,
       write one line inside the budget.
    8. QWEN PROMPTS — write them the way Qwen-Image-2.1 was trained to read:
       • EDIT (qwen_edit): lead with the operation, name ONLY what changes and push it
         hard ("Change the background to a warm sunset beach"), then ONE blanket
         preservation clause naming what stays by type and place ("keep the person, the
         logo, the headline text and the composition exactly as they are") — never
         describe a kept thing in detail, because that makes it drift. State it
         affirmatively; Qwen 2.1 is sampled without guidance, so "do not…" only belongs
         inside the sentence as an instruction, never as a separate negative prompt.
       • MULTI (qwen_multi): number the photos and give each a role ("reference image 1
         is the person's face; image 2 is the logo; each logo appears exactly once;
         do not confuse the references"). Point at a person by their reference number —
         describing a face in words makes the model regenerate and lose the likeness.
       • TEXT IN THE PICTURE: put the exact string in double quotes, character for
         character; never summarize, never hedge, never translate unless asked.
       • STICKER (qwen_sticker): include the frame sentence "This is an RGBA image with
         transparency. <what the sticker shows>. The image has alpha channel and the
         background is transparent." and describe the cut-out subject and its text.
       • Never put a resolution or aspect ratio in the prompt — the app sets the canvas.
    """

    /// the Create form state the planner inherits from unless the user's words say otherwise
    struct FormState: Codable {
        let prompt: String
        let quality: String
        let aspect: String
        let duration: Int
        let refMode: String
    }

    /// transcript + current form → plan. Throws typed errors; tailnet-down callers
    /// get a plain-words message via `friendly(_:)`.
    static func plan(transcript: String, hasPhotos: Int, hasVoice: Bool, current: FormState) async throws -> AssistPlan {
        let context: [String: Any] = [
            "transcript": transcript,
            "has_reference_photos": hasPhotos,
            "has_voice": hasVoice,
            "current": [
                "prompt": current.prompt,
                "quality": current.quality,
                "aspect": current.aspect,
                "duration": current.duration,
                "ref_mode": current.refMode,
            ],
        ]
        // the chat API wants a STRING here — sending the raw dict 400s at the
        // gateway (caught live 2026-09-09: the validator choked on our shape)
        guard let ctxJSON = String(data: try JSONSerialization.data(withJSONObject: context),
                                   encoding: .utf8) else {
            throw StudioError(status: nil, detail: "couldn't build the assist request")
        }
        let userContent = "Here is what I have and what I said, as JSON:\n\(ctxJSON)\n\nPlan it."
        let body: [String: Any] = [
            "model": model,
            "temperature": 0.3,
            // thinking LOW, per the user: planning is a formatting job, not a
            // reasoning one — and the gateway refuses reasoning_effort=off.
            "reasoning_effort": "low",
            "max_tokens": 800,
            "messages": [
                ["role": "system", "content": systemPrompt],
                ["role": "user", "content": userContent],
            ],
        ]
        var req = URLRequest(url: URL(string: "\(base)/chat/completions")!)
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, resp) = try await URLSession.shared.data(for: req)
        if let h = resp as? HTTPURLResponse, !(200 ..< 300).contains(h.statusCode) {
            let detail = String(data: data.prefix(200), encoding: .utf8) ?? "empty reply"
            throw StudioError(status: h.statusCode, detail: detail)
        }
        let chat = try JSONDecoder().decode(ChatCompletion.self, from: data)
        guard let content = chat.choices.first?.message.content else {
            throw StudioError(status: nil, detail: "the assistant sent an empty reply")
        }
        return try decodePlan(content)
    }

    /// lenient decode: strip markdown fences, clamp to the outermost braces
    static func decodePlan(_ content: String) throws -> AssistPlan {
        var t = content.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("```") {
            if let nl = t.firstIndex(of: "\n") { t = String(t[t.index(after: nl)...]) }
            if let fence = t.range(of: "```", options: .backwards) { t = String(t[..<fence.lowerBound]) }
        }
        if let a = t.firstIndex(of: "{"), let b = t.lastIndex(of: "}") {
            t = String(t[a ... b])
        }
        guard let d = t.data(using: .utf8) else {
            throw StudioError(status: nil, detail: "the assistant's plan was empty")
        }
        do {
            return try JSONDecoder().decode(AssistPlan.self, from: d)
        } catch {
            throw StudioError(status: nil, detail: "the assistant's plan wasn't valid JSON — try again")
        }
    }

    /// planner/network failures in plain words — tailnet-down gets its own line
    static func friendly(_ e: URLError) -> String {
        switch e.code {
        case .timedOut, .cannotConnectToHost, .cannotFindHost,
             .networkConnectionLost, .notConnectedToInternet, .dnsLookupFailed:
            return "AI assist needs Tailscale on — enable it and retry"
        default:
            return API.friendlyURLError(e)
        }
    }
}

/// the OpenAI-compatible chat reply shape (only what we need)
struct ChatCompletion: Codable {
    struct Choice: Codable {
        struct Msg: Codable { let content: String? }
        let message: Msg
    }
    let choices: [Choice]
}
