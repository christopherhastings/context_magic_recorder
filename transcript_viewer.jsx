import { useState, useMemo, useRef, useEffect } from "react";

// ── Sample data mirroring the recorder's JSON schema ─────────────────────────
const SAMPLE_TRANSCRIPTS = [
  {
    recording: { file: "/ZoomRecordings/2026-02-17_09-02-15_Weekly_Sync.wav", processed_at: "2026-02-17T11:04:00Z" },
    meeting: {
      topic: "Weekly Sync",
      start_time: "2026-02-17T09:02:00Z",
      duration_minutes: 62,
      agenda: "Q4 retrospective, roadmap planning, and team updates",
      host: { name: "Christopher Lowndes", email: "christopher@thoughtworks.com" },
      participants: [
        { name: "Christopher Lowndes", email: "christopher@thoughtworks.com", duration_sec: 3720 },
        { name: "Alice Kim", email: "alice@thoughtworks.com", duration_sec: 3600 },
        { name: "Bob Okafor", email: "bob@thoughtworks.com", duration_sec: 3480 },
        { name: "Sara Patel", email: "sara@thoughtworks.com", duration_sec: 2100 },
      ],
    },
    speakers: ["Christopher Lowndes", "Alice Kim", "Bob Okafor", "Sara Patel"],
    transcript: {
      turns: [
        { speaker: "Christopher Lowndes", start: 4.2, end: 28.1, text: "Morning everyone. Let's get started — I know some people have hard stops at ten. Quick agenda: we'll do Q4 retro first, then look at the roadmap for next quarter, then any team updates." },
        { speaker: "Alice Kim", start: 29.0, end: 52.4, text: "Before we start, can we spend a few minutes on the deployment issue from Friday? It blocked three client environments and I think we need to talk about the process." },
        { speaker: "Christopher Lowndes", start: 53.1, end: 71.8, text: "Absolutely. Bob, you were in the thick of that on Friday — do you want to walk us through what happened?" },
        { speaker: "Bob Okafor", start: 72.5, end: 140.3, text: "Yeah, so the short version is that the environment config wasn't being validated before the pipeline ran. We had a missing variable in staging that caused the whole deployment to fail silently. The frustrating part is that we actually have a check for this in the pre-deploy hook, but it wasn't triggered because someone had bypassed it for a hotfix two weeks ago and it never got re-enabled." },
        { speaker: "Alice Kim", start: 141.0, end: 172.5, text: "And the notification didn't fire either, which is why we didn't catch it for three hours. I think we need to review our alerting setup. The current threshold is way too conservative — we only get paged when things have been broken for more than two hours, which is clearly not working." },
        { speaker: "Sara Patel", start: 173.2, end: 201.8, text: "I can own the alerting review. I've been meaning to look at that anyway for the Memoria integration. We should probably also document what happened and add it to the runbook so the next person on call knows what to look for." },
        { speaker: "Christopher Lowndes", start: 202.5, end: 241.0, text: "Great. Sara, can you put together a short write-up by end of week? Nothing too formal, just enough to capture what happened and what we're changing. And Bob, can you make sure the pre-deploy hook is re-enabled and add a test for it so we know it's always active going forward?" },
        { speaker: "Bob Okafor", start: 241.8, end: 258.3, text: "Already done on the hook. I'll write the test today." },
        { speaker: "Christopher Lowndes", start: 259.1, end: 290.7, text: "Perfect. Okay, let's move to the Q4 retro. I put together some numbers earlier this week. We shipped six features in Q4, which is up from four in Q3. Three of them hit their original deadlines, two slipped by a week, and one is still in review. Overall I'd call it a good quarter with some process things to tighten up." },
        { speaker: "Alice Kim", start: 291.5, end: 335.2, text: "The AI Champion rollout was the big win for me. We went from zero to fourteen embedded champions across client accounts in eight weeks. Some of them are already generating really compelling case studies. The Champbot feedback loop is starting to give us data we can actually act on." },
      ],
    },
  },
  {
    recording: { file: "/ZoomRecordings/2026-02-14_14-00-00_AI_Champion_Program.wav", processed_at: "2026-02-14T16:12:00Z" },
    meeting: {
      topic: "AI Champion Program — Q1 Planning",
      start_time: "2026-02-14T14:00:00Z",
      duration_minutes: 48,
      agenda: "Review champion feedback data, plan Q1 expansion, define success metrics",
      host: { name: "Christopher Lowndes", email: "christopher@thoughtworks.com" },
      participants: [
        { name: "Christopher Lowndes", email: "christopher@thoughtworks.com", duration_sec: 2880 },
        { name: "Maria Gonzalez", email: "maria@thoughtworks.com", duration_sec: 2700 },
        { name: "James Wu", email: "james@thoughtworks.com", duration_sec: 2400 },
      ],
    },
    speakers: ["Christopher Lowndes", "Maria Gonzalez", "James Wu"],
    transcript: {
      turns: [
        { speaker: "Christopher Lowndes", start: 3.1, end: 42.8, text: "Thanks for joining. I wanted to use this session to look at what the Champbot data is telling us and figure out our Q1 expansion plan. We've got responses from eleven champions so far and some of the patterns are really interesting." },
        { speaker: "Maria Gonzalez", start: 43.5, end: 89.2, text: "I've been through the feedback and the standout finding for me is the confidence gap. Champions feel technically prepared — they can explain the tools, they can demo them — but they're struggling with the organizational side. How do you get a skeptical client to actually adopt something when their team is resistant?" },
        { speaker: "James Wu", start: 90.1, end: 134.6, text: "That matches what I'm hearing from the accounts I talk to. It's not a knowledge problem, it's a change management problem. The champions who are succeeding are the ones who found a sympathetic manager early and got small wins on the board quickly. The ones struggling are trying to change culture top-down without enough executive cover." },
        { speaker: "Christopher Lowndes", start: 135.4, end: 178.0, text: "So what's the implication for how we train and support champions? It sounds like we need to add something around stakeholder management and how to find and cultivate internal sponsors. That's different from anything we've done in the curriculum so far." },
        { speaker: "Maria Gonzalez", start: 178.8, end: 220.5, text: "Completely agree. I'd also add that we need to give them better templates for measuring and communicating impact. The clients that are skeptical — their main question is always 'show me the ROI' and right now champions are kind of improvising their own frameworks which leads to inconsistent stories." },
        { speaker: "James Wu", start: 221.3, end: 261.7, text: "What if we built a standard impact dashboard? Something champions can populate with their own data but that produces a consistent output. Even a simple before-and-after on developer velocity or code review time would be compelling if it's consistently measured across accounts." },
        { speaker: "Christopher Lowndes", start: 262.5, end: 308.1, text: "I love that idea. That's something we could probably build quickly — maybe tie it into the existing Champbot infrastructure. James, would you be willing to sketch out what that might look like? Even just a one-pager on what metrics we'd track and how we'd collect them." },
      ],
    },
  },
  {
    recording: { file: "/ZoomRecordings/2026-02-10_10-30-00_Memoria_Journal.wav", processed_at: "2026-02-10T12:45:00Z" },
    meeting: {
      topic: "Memoria Journal — Voice Feature Architecture",
      start_time: "2026-02-10T10:30:00Z",
      duration_minutes: 35,
      agenda: "ADK integration, voice pipeline design, privacy considerations",
      host: { name: "Christopher Lowndes", email: "christopher@thoughtworks.com" },
      participants: [
        { name: "Christopher Lowndes", email: "christopher@thoughtworks.com", duration_sec: 2100 },
        { name: "Dev Partner", email: "dev@partner.com", duration_sec: 1980 },
      ],
    },
    speakers: ["Christopher Lowndes", "Dev Partner"],
    transcript: {
      turns: [
        { speaker: "Christopher Lowndes", start: 2.5, end: 45.8, text: "So the core idea is that Memoria users should be able to just talk to the app instead of typing. You wake it up, it asks you a question about your day or week, you respond, and it weaves your answer into the journal entry alongside what your partner wrote. The AI layer handles the synthesis." },
        { speaker: "Dev Partner", start: 46.5, end: 98.2, text: "The ADK integration makes a lot of sense for that. You'd basically have a voice agent that handles the conversation loop — listening, generating follow-up prompts, then passing the transcript to your synthesis layer. The tricky part is knowing when to stop listening. You want it to feel conversational, not like leaving a voicemail." },
        { speaker: "Christopher Lowndes", start: 99.0, end: 142.7, text: "Right, and the privacy side is really important to me. Memoria's whole value proposition is that it's a private family space. I don't want voice recordings being stored anywhere they don't need to be. Ideally the audio is transcribed locally and only the text ever leaves the device." },
        { speaker: "Dev Partner", start: 143.5, end: 195.1, text: "That's very achievable. Whisper runs well on-device, you can do the STT locally, send the transcript to your API, generate the response, then do TTS on the other end. The only thing leaving the device is text. You could even do the synthesis on-device if you're willing to accept a smaller model." },
        { speaker: "Christopher Lowndes", start: 195.9, end: 238.4, text: "That's the direction I want to go. Let's design it so the privacy-preserving path is the default and cloud processing is opt-in for users who want better quality. I think most parents would trade some quality for knowing their kids' voices aren't being sent to a server somewhere." },
      ],
    },
  },
];

// ── Helpers ───────────────────────────────────────────────────────────────────
const SPEAKER_COLORS = [
  "#e8a45a", "#7eb8d4", "#a8d490", "#d490a8", "#d4c490", "#90a8d4",
];

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  });
}

function formatDuration(minutes) {
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function highlight(text, query) {
  if (!query.trim()) return text;
  const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
  const parts = text.split(regex);
  return parts.map((part, i) =>
    regex.test(part)
      ? <mark key={i} style={{ background: "#e8a45a33", color: "#e8a45a", borderRadius: "2px", padding: "0 1px" }}>{part}</mark>
      : part
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function TranscriptListItem({ transcript, isActive, onClick, searchQuery }) {
  const { meeting, transcript: t } = transcript;
  const matchCount = useMemo(() => {
    if (!searchQuery.trim()) return 0;
    const q = searchQuery.toLowerCase();
    return t.turns.filter(turn => turn.text.toLowerCase().includes(q)).length;
  }, [searchQuery, t.turns]);

  return (
    <button
      onClick={onClick}
      style={{
        display: "block", width: "100%", textAlign: "left",
        padding: "16px 20px",
        background: isActive ? "rgba(232,164,90,0.08)" : "transparent",
        border: "none",
        borderLeft: isActive ? "2px solid #e8a45a" : "2px solid transparent",
        borderBottom: "1px solid rgba(255,255,255,0.05)",
        cursor: "pointer",
        transition: "all 0.15s ease",
      }}
      onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = "rgba(255,255,255,0.03)"; }}
      onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
    >
      <div style={{ fontSize: "13px", fontWeight: 600, color: isActive ? "#e8a45a" : "#e2d5c3", marginBottom: 4, lineHeight: 1.3, fontFamily: "'DM Serif Display', Georgia, serif" }}>
        {meeting.topic}
      </div>
      <div style={{ fontSize: "11px", color: "#8a7d6e", marginBottom: 6 }}>
        {formatDate(meeting.start_time)} · {formatDuration(meeting.duration_minutes)}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: "10px", color: "#6a5f52", letterSpacing: "0.05em" }}>
          {meeting.participants.length} participants
        </span>
        {matchCount > 0 && (
          <span style={{
            fontSize: "10px", background: "rgba(232,164,90,0.15)",
            color: "#e8a45a", padding: "1px 6px", borderRadius: "10px",
            letterSpacing: "0.03em",
          }}>
            {matchCount} match{matchCount !== 1 ? "es" : ""}
          </span>
        )}
      </div>
    </button>
  );
}

function ParticipantBadge({ name, duration, color }) {
  const initials = name.split(" ").map(w => w[0]).join("").slice(0, 2);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0" }}>
      <div style={{
        width: 28, height: 28, borderRadius: "50%", background: `${color}22`,
        border: `1px solid ${color}44`, display: "flex", alignItems: "center",
        justifyContent: "center", fontSize: "10px", color, fontWeight: 700,
        flexShrink: 0,
      }}>
        {initials}
      </div>
      <div>
        <div style={{ fontSize: "12px", color: "#c8bdb0", fontWeight: 500 }}>{name}</div>
        {duration && <div style={{ fontSize: "10px", color: "#6a5f52" }}>{Math.floor(duration / 60)}m</div>}
      </div>
    </div>
  );
}

function TurnBlock({ turn, speakerColor, searchQuery, isHighlighted }) {
  const ref = useRef();
  useEffect(() => {
    if (isHighlighted && ref.current) {
      ref.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [isHighlighted]);

  return (
    <div
      ref={ref}
      style={{
        display: "grid", gridTemplateColumns: "140px 1fr", gap: "0 24px",
        padding: "16px 0",
        borderBottom: "1px solid rgba(255,255,255,0.04)",
        background: isHighlighted ? "rgba(232,164,90,0.04)" : "transparent",
        transition: "background 0.3s",
        borderRadius: isHighlighted ? "4px" : 0,
      }}
    >
      <div style={{ paddingTop: 2 }}>
        <div style={{
          fontSize: "11px", fontWeight: 700, color: speakerColor,
          textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4,
        }}>
          {turn.speaker.split(" ")[0]}
        </div>
        <div style={{
          fontSize: "10px", color: "#5a4f44",
          fontFamily: "'DM Mono', 'Courier New', monospace",
        }}>
          {formatTime(turn.start)}
        </div>
      </div>
      <div style={{
        fontSize: "14px", color: "#c8bdb0", lineHeight: 1.75,
        fontFamily: "'Lora', Georgia, serif",
      }}>
        {searchQuery ? highlight(turn.text, searchQuery) : turn.text}
      </div>
    </div>
  );
}

function TranscriptView({ transcript, searchQuery }) {
  const { meeting, speakers, transcript: t } = transcript;

  const speakerColorMap = useMemo(() => {
    const map = {};
    speakers.forEach((s, i) => { map[s] = SPEAKER_COLORS[i % SPEAKER_COLORS.length]; });
    return map;
  }, [speakers]);

  const matchedTurnIndices = useMemo(() => {
    if (!searchQuery.trim()) return new Set();
    const q = searchQuery.toLowerCase();
    return new Set(t.turns.map((turn, i) => turn.text.toLowerCase().includes(q) ? i : -1).filter(i => i !== -1));
  }, [searchQuery, t.turns]);

  const [highlightIndex, setHighlightIndex] = useState(null);
  const matchArray = [...matchedTurnIndices];

  useEffect(() => {
    if (matchArray.length > 0) setHighlightIndex(matchArray[0]);
    else setHighlightIndex(null);
  }, [searchQuery]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {/* Header */}
      <div style={{
        padding: "28px 36px 20px",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
        flexShrink: 0,
      }}>
        <div style={{ fontSize: "11px", color: "#6a5f52", letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 8 }}>
          {formatDate(meeting.start_time)} · {formatDuration(meeting.duration_minutes)}
        </div>
        <h1 style={{ fontFamily: "'DM Serif Display', Georgia, serif", fontSize: "26px", color: "#e2d5c3", fontWeight: 400, margin: "0 0 12px", lineHeight: 1.2 }}>
          {meeting.topic}
        </h1>
        {meeting.agenda && (
          <p style={{ fontSize: "13px", color: "#8a7d6e", margin: "0 0 16px", lineHeight: 1.6, maxWidth: "600px", fontStyle: "italic" }}>
            {meeting.agenda}
          </p>
        )}

        {/* Participants row */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0 24px" }}>
          {meeting.participants.map((p, i) => (
            <ParticipantBadge
              key={p.email}
              name={p.name}
              duration={p.duration_sec}
              color={speakerColorMap[p.name] || SPEAKER_COLORS[i % SPEAKER_COLORS.length]}
            />
          ))}
        </div>

        {/* Match navigation */}
        {matchArray.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 16 }}>
            <span style={{ fontSize: "11px", color: "#8a7d6e" }}>
              {matchArray.length} match{matchArray.length !== 1 ? "es" : ""} for "{searchQuery}"
            </span>
            <div style={{ display: "flex", gap: 4 }}>
              {[
                { label: "↑", action: () => {
                  const idx = matchArray.indexOf(highlightIndex);
                  setHighlightIndex(matchArray[(idx - 1 + matchArray.length) % matchArray.length]);
                }},
                { label: "↓", action: () => {
                  const idx = matchArray.indexOf(highlightIndex);
                  setHighlightIndex(matchArray[(idx + 1) % matchArray.length]);
                }},
              ].map(({ label, action }) => (
                <button key={label} onClick={action} style={{
                  background: "rgba(255,255,255,0.06)", border: "none", color: "#8a7d6e",
                  width: 22, height: 22, borderRadius: 4, cursor: "pointer", fontSize: "11px",
                }}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Turns */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px 36px 40px" }}>
        {t.turns.map((turn, i) => (
          <TurnBlock
            key={i}
            turn={turn}
            speakerColor={speakerColorMap[turn.speaker] || "#8a7d6e"}
            searchQuery={searchQuery}
            isHighlighted={i === highlightIndex}
          />
        ))}
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────

export default function App() {
  const [searchQuery, setSearchQuery] = useState("");
  const [activeId, setActiveId] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const filteredTranscripts = useMemo(() => {
    const q = searchQuery.toLowerCase().trim();
    if (!q) return SAMPLE_TRANSCRIPTS.map((t, i) => ({ ...t, _id: i }));
    return SAMPLE_TRANSCRIPTS
      .map((t, i) => ({ ...t, _id: i }))
      .filter(t =>
        t.meeting.topic.toLowerCase().includes(q) ||
        t.meeting.agenda?.toLowerCase().includes(q) ||
        t.transcript.turns.some(turn => turn.text.toLowerCase().includes(q)) ||
        t.meeting.participants.some(p => p.name.toLowerCase().includes(q))
      );
  }, [searchQuery]);

  const activeTranscript = SAMPLE_TRANSCRIPTS[activeId] || SAMPLE_TRANSCRIPTS[0];

  return (
    <div style={{
      height: "100vh", display: "flex", flexDirection: "column",
      background: "#1a1610", color: "#e2d5c3",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&family=Lora:ital,wght@0,400;1,400&display=swap');
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #3a332a; border-radius: 2px; }
        ::-webkit-scrollbar-thumb:hover { background: #4a4338; }
        button:focus { outline: none; }
        mark { background: rgba(232,164,90,0.25) !important; color: #e8a45a !important; }
      `}</style>

      {/* Top bar */}
      <div style={{
        display: "flex", alignItems: "center", gap: 16,
        padding: "12px 20px",
        background: "#141210",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
        flexShrink: 0,
        zIndex: 10,
      }}>
        <button
          onClick={() => setSidebarOpen(o => !o)}
          style={{ background: "none", border: "none", color: "#6a5f52", cursor: "pointer", padding: 4, fontSize: 16 }}
          title="Toggle sidebar"
        >
          ☰
        </button>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{
            width: 8, height: 8, borderRadius: "50%", background: "#e8a45a",
            boxShadow: "0 0 6px #e8a45a88",
          }} />
          <span style={{
            fontFamily: "'DM Mono', monospace", fontSize: "12px",
            color: "#8a7d6e", letterSpacing: "0.06em",
          }}>
            TRANSCRIPTS
          </span>
        </div>

        {/* Search */}
        <div style={{ flex: 1, maxWidth: 480, position: "relative" }}>
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="Search across all transcripts..."
            style={{
              width: "100%", padding: "7px 14px 7px 34px",
              background: "rgba(255,255,255,0.04)",
              border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: 6, color: "#c8bdb0", fontSize: "13px",
              outline: "none", transition: "border-color 0.15s",
              fontFamily: "'DM Sans', sans-serif",
            }}
            onFocus={e => e.target.style.borderColor = "rgba(232,164,90,0.4)"}
            onBlur={e => e.target.style.borderColor = "rgba(255,255,255,0.08)"}
          />
          <span style={{ position: "absolute", left: 11, top: "50%", transform: "translateY(-50%)", color: "#5a4f44", fontSize: 13 }}>
            ⌕
          </span>
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              style={{
                position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
                background: "none", border: "none", color: "#6a5f52", cursor: "pointer",
                fontSize: 12, padding: "2px 4px",
              }}
            >✕</button>
          )}
        </div>

        <div style={{ marginLeft: "auto", fontSize: "11px", color: "#4a4338", fontFamily: "'DM Mono', monospace" }}>
          {filteredTranscripts.length}/{SAMPLE_TRANSCRIPTS.length} meetings
        </div>
      </div>

      {/* Body */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* Sidebar */}
        {sidebarOpen && (
          <div style={{
            width: 260, flexShrink: 0,
            borderRight: "1px solid rgba(255,255,255,0.06)",
            overflowY: "auto",
            background: "#16130f",
          }}>
            {filteredTranscripts.length === 0 ? (
              <div style={{ padding: "40px 20px", textAlign: "center", color: "#4a4338", fontSize: "12px" }}>
                No transcripts match your search
              </div>
            ) : (
              filteredTranscripts.map(transcript => (
                <TranscriptListItem
                  key={transcript._id}
                  transcript={transcript}
                  isActive={activeId === transcript._id}
                  onClick={() => setActiveId(transcript._id)}
                  searchQuery={searchQuery}
                />
              ))
            )}
          </div>
        )}

        {/* Main content */}
        <div style={{ flex: 1, overflow: "hidden" }}>
          {activeTranscript
            ? <TranscriptView transcript={activeTranscript} searchQuery={searchQuery} />
            : (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#4a4338", fontSize: "13px" }}>
                Select a transcript to read
              </div>
            )
          }
        </div>
      </div>
    </div>
  );
}
