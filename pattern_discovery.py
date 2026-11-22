"""Conservative pattern discovery over selected, normalized evidence.

Local mining is deliberately heuristic. AI suggestions can be imported from an
offline/model-generated JSON file, but are never sent to a model here and are
never published without an explicit human review call.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path

from evidence_processing import apply_migrations

ROOT = Path(__file__).resolve().parent
DB = ROOT / "sentinel.db"
DISCOVERY_VERSION = "selected_evidence_miner_v2"
MIN_SUPPORT = 2
MAX_LOCAL_CANDIDATES = 200
DUPLICATE_SIMILARITY_THRESHOLD = 90.0
WORD_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
NAME_RE = re.compile(r"[A-ZİĞÜŞÖÇ][^\W_]*(?:['’.-][^\W_]+)?", flags=re.UNICODE)
ALLOWED_TYPES = {"company_brand", "service_description", "recurring_phrase", "business_terminology", "other"}


def normalize_phrase(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(WORD_RE.findall(value))


def _language(value):
    value = (value or "und").strip().lower().replace("_", "-")
    if "+" in value or "," in value:
        return "und"
    if value in ("tr", "tur", "turkish"):
        return "tr"
    if value in ("en", "eng", "english"):
        return "en"
    return value or "und"


def _fingerprint(candidate_type, language, normalized):
    payload = json.dumps([candidate_type, language, normalized], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _excerpt_for_tokens(raw_text, wanted):
    matches = list(WORD_RE.finditer(raw_text or ""))
    tokens = [normalize_phrase(m.group()) for m in matches]
    for start in range(0, len(tokens) - len(wanted) + 1):
        if tokens[start:start + len(wanted)] == wanted:
            return (raw_text or "")[matches[start].start():matches[start + len(wanted) - 1].end()]
    return None


def _connect(db_path=DB):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        apply_migrations(conn)
    except Exception:
        conn.close()
        raise
    return conn


def selected_evidence(conn):
    """Return only current selected, quality-valid, normalized evidence."""
    return conn.execute("""SELECT i.candidate_id,i.source_artifact_id,i.advertisement_id,
        i.modality,i.original_text,i.normalized_text,i.normalization_version,
        c.language,c.raw_output,c.frame_timestamp_seconds,c.interval_start_seconds,c.interval_end_seconds
      FROM current_selected_matching_input i
      JOIN evidence_candidates c ON c.candidate_id=i.candidate_id
      ORDER BY i.advertisement_id,i.source_artifact_id,i.candidate_id""").fetchall()


def _start_run(conn, source_count, mode):
    run_id = str(uuid.uuid4())
    conn.execute("INSERT INTO pattern_discovery_runs(run_id,status,discovery_version,input_count,config_json) VALUES (?,'running',?,?,?)",
                 (run_id, DISCOVERY_VERSION, source_count, json.dumps({"mode": mode, "source": "current_selected_matching_input"}, sort_keys=True)))
    conn.commit()
    return run_id


def _library_terms(conn):
    terms = []
    for row in conn.execute("SELECT id,canonical_name FROM patterns WHERE is_active=1 ORDER BY id"):
        terms.append((row["id"], None, normalize_phrase(row["canonical_name"].replace("_", " "))))
    for row in conn.execute("""SELECT p.id,v.language,v.variant_text FROM pattern_variants v
      JOIN patterns p ON p.id=v.pattern_id WHERE p.is_active=1 ORDER BY p.id,v.id"""):
        terms.append((row["id"], _language(row["language"]), normalize_phrase(row["variant_text"])))
    return [(pid, lang, norm) for pid, lang, norm in terms if norm]


def _find_pattern_duplicate(conn, normalized, language):
    best = None
    for pattern_id, term_language, term in _library_terms(conn):
        if term_language and language not in ("und", term_language):
            continue
        score = 100.0 if normalized == term else SequenceMatcher(None, normalized, term).ratio() * 100
        if best is None or score > best[0]:
            best = (score, pattern_id)
    if best and best[0] >= DUPLICATE_SIMILARITY_THRESHOLD:
        return ("duplicate" if best[0] == 100 else "possible_duplicate", best[1], round(best[0], 2))
    return None


def _find_candidate_duplicate(conn, normalized, language, candidate_type):
    best = None
    for row in conn.execute("""SELECT candidate_id,normalized_text,language FROM pattern_discovery_candidates
      WHERE discovery_status!='rejected'"""):
        if _language(row["language"]) != language:
            continue
        score = 100.0 if normalized == row["normalized_text"] else SequenceMatcher(None, normalized, row["normalized_text"]).ratio() * 100
        if best is None or score > best[0]:
            best = (score, row["candidate_id"])
    if best and best[0] >= DUPLICATE_SIMILARITY_THRESHOLD:
        return ("duplicate" if best[0] == 100 else "possible_duplicate", best[1], round(best[0], 2))
    return None


def _persist_candidate(conn, run_id, draft, proposed_by, proposal_json=None):
    text = str(draft.get("proposed_text") or "").strip()
    language = _language(draft.get("language"))
    candidate_type = draft.get("candidate_type", "other")
    normalized = normalize_phrase(text)
    if not normalized or candidate_type not in ALLOWED_TYPES:
        raise ValueError("Candidate needs nonempty text and a supported candidate_type")
    fingerprint = _fingerprint(candidate_type, language, normalized)
    if proposed_by=="ai_assisted":
        # Keep AI proposal identity distinct from heuristic leads and make its
        # stated relationship to the library part of its idempotency key.
        fingerprint=hashlib.sha256(json.dumps([fingerprint,draft.get("novelty_type"),draft.get("target_pattern_id")],
            ensure_ascii=False,separators=(",",":")).encode("utf-8")).hexdigest()
    candidate_id = fingerprint
    existing = conn.execute("SELECT * FROM pattern_discovery_candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
    confidence = float(draft["confidence"])
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    status, duplicate_pattern_id, duplicate_candidate_id = "pending_review", None, None
    duplicate = _find_pattern_duplicate(conn, normalized, language)
    if duplicate:
        status = duplicate[0]
        duplicate_pattern_id = duplicate[1]
    else:
        duplicate = _find_candidate_duplicate(conn, normalized, language, candidate_type)
        if duplicate:
            status = duplicate[0]
            duplicate_candidate_id = duplicate[1]
    if draft.get("novelty_type")=="possible_duplicate" and status=="pending_review":
        status="possible_duplicate"
        duplicate_pattern_id=draft.get("target_pattern_id")

    if existing:
        candidate_id = existing["candidate_id"]
        # Preserve reviewed/approved state and the first confidence rationale.
        conn.execute("""UPDATE pattern_discovery_candidates SET last_seen_run_id=?,is_current=1,
          novelty_type=COALESCE(novelty_type,?),reasoning=COALESCE(reasoning,?),target_pattern_id=COALESCE(target_pattern_id,?),
          updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?""",
                     (run_id,draft.get("novelty_type"),draft.get("reasoning"),draft.get("target_pattern_id"),candidate_id))
    else:
        conn.execute("""INSERT INTO pattern_discovery_candidates(
          candidate_id,fingerprint,proposed_text,normalized_text,language,candidate_type,description,confidence,
          confidence_basis,discovery_status,duplicate_pattern_id,duplicate_candidate_id,suggested_category_key,
          proposed_by,proposal_json,first_run_id,last_seen_run_id,novelty_type,reasoning,target_pattern_id)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (candidate_id,fingerprint,text,normalized,draft.get("language") or language,candidate_type,draft.get("description"),
           confidence,draft.get("confidence_basis") or "unverified discovery score",status,duplicate_pattern_id,duplicate_candidate_id,
           draft.get("suggested_category_key"),proposed_by,json.dumps(proposal_json or {},ensure_ascii=False,sort_keys=True),run_id,run_id,
           draft.get("novelty_type"),draft.get("reasoning"),draft.get("target_pattern_id")))
    conn.execute("INSERT OR IGNORE INTO pattern_discovery_candidate_runs(run_id,candidate_id,confidence,rationale) VALUES (?,?,?,?)",
                 (run_id,candidate_id,confidence,draft.get("rationale") or "Candidate mined from selected evidence"))
    for evidence in draft.get("evidence", []):
        excerpt = str(evidence.get("evidence_excerpt") or "")
        source_candidate_id = evidence.get("source_candidate_id")
        source_artifact_id = evidence.get("source_artifact_id")
        if not excerpt or not source_candidate_id or source_artifact_id is None:
            raise ValueError("Each supporting evidence item needs source_candidate_id, source_artifact_id and exact evidence_excerpt")
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        conn.execute("""INSERT OR IGNORE INTO pattern_discovery_candidate_evidence
          (candidate_id,source_candidate_id,source_artifact_id,evidence_excerpt,excerpt_hash,first_run_id)
          VALUES (?,?,?,?,?,?)""",(candidate_id,source_candidate_id,source_artifact_id,excerpt,excerpt_hash,run_id))
    return candidate_id


def _mine_local(rows):
    buckets = {}
    token_sources={}
    for row in rows:
        for token in set(WORD_RE.findall(row["normalized_text"] or "")):
            token_sources.setdefault(token,set()).add(row["candidate_id"])
    rare_name_token_limit=max(4,int(len(rows)*0.12))
    for row in rows:
        raw = row["original_text"] or ""
        language = _language(row["language"])
        tokens = WORD_RE.findall(row["normalized_text"] or "")
        for size in range(2, min(5, len(tokens)) + 1):
            for i in range(len(tokens) - size + 1):
                phrase_tokens = tokens[i:i + size]
                if not any(len(token) >= 4 and not token.isdigit() for token in phrase_tokens):
                    continue
                normalized = " ".join(phrase_tokens)
                excerpt = _excerpt_for_tokens(raw, phrase_tokens)
                if not excerpt:
                    continue
                key = ("recurring_phrase", language, normalized)
                item = buckets.setdefault(key, {"proposed_text": excerpt,"language":language,"evidence":{},"type":"recurring_phrase"})
                item["evidence"].setdefault(row["candidate_id"], {"source_candidate_id":row["candidate_id"],"source_artifact_id":row["source_artifact_id"],"evidence_excerpt":excerpt,"advertisement_id":row["advertisement_id"]})

        # Capitalized multiword spans are entity-name leads, not asserted NER.
        raw_words = list(NAME_RE.finditer(raw))
        for start in range(len(raw_words)):
            for size in (2,3):
                end = start + size
                if end > len(raw_words):
                    continue
                between = raw[raw_words[start].end():raw_words[end-1].start()]
                if not between or not between.isspace():
                    continue
                excerpt = raw[raw_words[start].start():raw_words[end-1].end()]
                normalized = normalize_phrase(excerpt)
                if len(normalized.split()) < 2:
                    continue
                key = ("company_brand", language, normalized)
                item = buckets.setdefault(key,{"proposed_text":excerpt,"language":language,"evidence":{},"type":"company_brand"})
                item["evidence"].setdefault(row["candidate_id"],{"source_candidate_id":row["candidate_id"],"source_artifact_id":row["source_artifact_id"],"evidence_excerpt":excerpt,"advertisement_id":row["advertisement_id"]})

    drafts=[]
    for (candidate_type,language,normalized),item in buckets.items():
        evidence=list(item["evidence"].values())
        if len(evidence)<MIN_SUPPORT:
            continue
        # Repeated title-case copy across most frames is more plausibly a video
        # title than a proper-name lead. Keep the entity heuristic conservative;
        # such spans can still surface as recurring_phrase candidates.
        if candidate_type=="company_brand" and len(evidence)>6:
            continue
        if candidate_type=="company_brand" and any(len(token_sources.get(token,set()))>rare_name_token_limit for token in normalized.split()):
            continue
        ad_count=len({e["advertisement_id"] for e in evidence})
        score=min(0.65,0.25+0.05*min(len(evidence)-1,6))
        if ad_count>1:
            score=min(0.8,score+0.15)
        if candidate_type=="company_brand":
            score=min(0.7,score+0.05)
        basis=f"heuristic_v1; {len(evidence)} selected evidence records; {ad_count} advertisements; uncalibrated"
        drafts.append({"proposed_text":item["proposed_text"],"language":language,"candidate_type":candidate_type,
          "description":"Unsupervised phrase/name lead; requires semantic and source review.","confidence":round(score,2),
          "confidence_basis":basis,"rationale":basis,"evidence":evidence})
    drafts.sort(key=lambda d:(-len(d["evidence"]),d["candidate_type"],normalize_phrase(d["proposed_text"])))
    return drafts[:MAX_LOCAL_CANDIDATES]


def run_local_discovery(db_path=DB):
    conn=_connect(db_path)
    rows=selected_evidence(conn)
    run_id=_start_run(conn,len(rows),"local_heuristic")
    try:
        drafts=_mine_local(rows)
        conn.execute("BEGIN IMMEDIATE")
        # Retain old hypotheses as history, but keep the active review queue
        # aligned with this miner version. Fingerprints rediscovered below are
        # reactivated by _persist_candidate.
        conn.execute("UPDATE pattern_discovery_candidates SET is_current=0,updated_at=CURRENT_TIMESTAMP WHERE proposed_by='local_discovery' AND is_current=1")
        for draft in drafts:
            _persist_candidate(conn,run_id,draft,"local_discovery")
        conn.execute("UPDATE pattern_discovery_runs SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE run_id=?",(run_id,))
        conn.commit()
        return {"run_id":run_id,"selected_evidence_count":len(rows),"discovered_candidates":len(drafts),
                "current_candidate_count":conn.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE is_current=1").fetchone()[0],
                "superseded_history_count":conn.execute("SELECT COUNT(*) FROM pattern_discovery_candidates WHERE is_current=0").fetchone()[0],
                "statuses":dict(conn.execute("SELECT discovery_status,COUNT(*) FROM pattern_discovery_candidates WHERE is_current=1 GROUP BY discovery_status"))}
    except Exception as error:
        conn.rollback()
        conn.execute("UPDATE pattern_discovery_runs SET status='failed',error_text=?,completed_at=CURRENT_TIMESTAMP WHERE run_id=?",(repr(error),run_id))
        conn.commit()
        raise
    finally:
        conn.close()


def export_ai_bundle(db_path=DB, output_path=None):
    """Export selected evidence and library context for an offline AI step."""
    conn=_connect(db_path)
    rows=selected_evidence(conn)
    patterns=[dict(r) for r in conn.execute("""SELECT p.id,p.canonical_name,p.description,p.is_active,
      GROUP_CONCAT(DISTINCT v.variant_text) AS variants
      FROM patterns p LEFT JOIN pattern_variants v ON v.pattern_id=p.id WHERE p.is_active=1 GROUP BY p.id ORDER BY p.id""")]
    bundle={"metadata":{"source":"current_selected_matching_input","selected_evidence_count":len(rows),
        "discovery_version":DISCOVERY_VERSION,"confidence_is_unverified":True},
      "approved_patterns":patterns,
      "evidence":[{"source_candidate_id":r["candidate_id"],"source_artifact_id":r["source_artifact_id"],
        "advertisement_id":r["advertisement_id"],"modality":r["modality"],"language":r["language"],
        "text":r["original_text"],"normalized_text":r["normalized_text"],"normalization_version":r["normalization_version"],
        "frame_timestamp_seconds":r["frame_timestamp_seconds"],"interval_start_seconds":r["interval_start_seconds"],
        "interval_end_seconds":r["interval_end_seconds"]} for r in rows]}
    conn.close()
    out=Path(output_path) if output_path else ROOT/"outputs"/"selected_pattern_discovery_bundle.json"
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"path":str(out),"selected_evidence_count":len(rows),"pattern_count":len(patterns)}


def list_candidates(db_path=DB, status=None, include_history=False):
    conn=_connect(db_path)
    query="SELECT candidate_id,proposed_text,language,candidate_type,confidence,confidence_basis,discovery_status,duplicate_pattern_id,duplicate_candidate_id,suggested_category_key,proposed_by,novelty_type,reasoning,target_pattern_id,created_at FROM pattern_discovery_candidates"
    clauses=[];args=[]
    if not include_history:
        clauses.append("is_current=1")
    if status:
        clauses.append("discovery_status=?");args.append(status)
    if clauses:
        query+=" WHERE "+" AND ".join(clauses)
    query+=" ORDER BY created_at,candidate_id"
    result=[]
    for row in conn.execute(query,args):
        item=dict(row)
        item["evidence"]=[dict(e) for e in conn.execute("""SELECT ce.source_candidate_id,ce.source_artifact_id,ce.evidence_excerpt,
          x.advertisement_id,x.modality,x.frame_timestamp_seconds,x.interval_start_seconds,x.interval_end_seconds
          FROM pattern_discovery_candidate_evidence ce
          JOIN evidence_candidates x ON x.candidate_id=ce.source_candidate_id
          WHERE ce.candidate_id=? ORDER BY x.advertisement_id,ce.source_artifact_id""",(item["candidate_id"],))]
        result.append(item)
    conn.close()
    return result


def get_candidate(candidate_id, db_path=DB):
    rows=list_candidates(db_path,include_history=True)
    return next((row for row in rows if row["candidate_id"]==candidate_id),None)


def import_ai_suggestions(suggestions, db_path=DB, run_metadata=None, existing_run_id=None):
    """Validate citations against current selected evidence, then persist suggestions separately."""
    if isinstance(suggestions,(str,Path)):
        if isinstance(suggestions,Path):
            raw=suggestions.read_text(encoding="utf-8")
        else:
            value=str(suggestions)
            raw=value if value.lstrip().startswith(("{","[")) else Path(value).read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 2_000_000:
            raise ValueError("AI suggestion response exceeds the 2 MB limit")
        try:
            suggestions=json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("AI response must be valid JSON without surrounding prose") from exc
    proposals=suggestions.get("proposals") if isinstance(suggestions,dict) else None
    if not isinstance(proposals,list):
        raise ValueError("AI suggestion input must contain a proposals list")
    try:
        response_size=len(json.dumps(suggestions,ensure_ascii=False).encode("utf-8"))
    except (TypeError,ValueError) as exc:
        raise ValueError("AI suggestion response must be JSON serializable") from exc
    if response_size > 2_000_000:
        raise ValueError("AI suggestion response exceeds the 2 MB limit")
    conn=_connect(db_path)
    try:
        active={r["candidate_id"]:r for r in selected_evidence(conn)}
        approved={r["id"] for r in conn.execute("SELECT id FROM patterns WHERE is_active=1")}
        validated=[]
        for proposal in proposals:
            if not isinstance(proposal,dict):
                raise ValueError("Each proposal must be an object")
            required=("candidate_name","candidate_category","supporting_evidence","reasoning","confidence","confidence_basis","novelty_type","target_pattern_id")
            missing=[key for key in required if key not in proposal]
            if missing:
                raise ValueError("AI proposal is missing required fields: "+", ".join(missing))
            name=proposal["candidate_name"]
            category=proposal["candidate_category"]
            reasoning=proposal["reasoning"]
            basis=proposal["confidence_basis"]
            novelty=proposal["novelty_type"]
            confidence=proposal["confidence"]
            if not isinstance(name,str) or not name.strip():
                raise ValueError("candidate_name must be nonempty text")
            if not isinstance(category,str) or category not in ALLOWED_TYPES:
                raise ValueError("candidate_category is not a supported discovery category")
            if not isinstance(reasoning,str) or not reasoning.strip() or not isinstance(basis,str) or not basis.strip():
                raise ValueError("reasoning and confidence_basis must be nonempty text")
            if not isinstance(novelty,str) or novelty not in ("new_pattern","variant","possible_duplicate"):
                raise ValueError("novelty_type must be new_pattern, variant or possible_duplicate")
            if not isinstance(confidence,(int,float)) or isinstance(confidence,bool) or not 0<=confidence<=1:
                raise ValueError("confidence must be a number between 0 and 1")
            target=proposal.get("target_pattern_id")
            if novelty=="new_pattern" and target is not None:
                raise ValueError("new_pattern suggestions must not specify target_pattern_id")
            if novelty in ("variant","possible_duplicate") and (isinstance(target,bool) or not isinstance(target,int) or target not in approved):
                raise ValueError("variant and possible_duplicate suggestions need an active approved target_pattern_id")
            if proposal.get("language") is not None and not isinstance(proposal["language"],str):
                raise ValueError("language must be a text language tag")
            category_key=proposal.get("suggested_category_key")
            if category_key is not None and (not isinstance(category_key,str) or category_key not in {r[0] for r in conn.execute("SELECT category_key FROM pattern_categories")}):
                raise ValueError("suggested_category_key must identify an existing library category")
            evidence=proposal["supporting_evidence"]
            if not isinstance(evidence,list) or not evidence:
                raise ValueError("Each proposal must cite at least one selected evidence excerpt")
            checked=[]
            for item in evidence:
                if not isinstance(item,dict):
                    raise ValueError("Each supporting_evidence entry must be an object")
                source_id=item.get("source_candidate_id")
                artifact_id=item.get("source_artifact_id")
                if not isinstance(source_id,str) or not source_id:
                    raise ValueError("source_candidate_id must identify a selected evidence candidate")
                source=active.get(source_id)
                if isinstance(artifact_id,bool) or not isinstance(artifact_id,int) or not source or source["source_artifact_id"]!=artifact_id:
                    raise ValueError("AI proposal references evidence outside current selected evidence")
                excerpt=item.get("exact_source_excerpt")
                if not isinstance(excerpt,str) or not excerpt or excerpt not in (source["original_text"] or ""):
                    raise ValueError("exact_source_excerpt must exactly occur in the cited selected source")
                checked.append({"source_candidate_id":source["candidate_id"],"source_artifact_id":source["source_artifact_id"],"evidence_excerpt":excerpt})
            validated.append({**proposal,"candidate_name":name.strip(),"candidate_category":category,
                "supporting_evidence":checked,"confidence":float(confidence),"target_pattern_id":target})
    except Exception:
        conn.close()
        raise
    if existing_run_id:
        run=conn.execute("SELECT status FROM pattern_discovery_runs WHERE run_id=?",(existing_run_id,)).fetchone()
        if not run or run["status"]!="running":
            conn.close()
            raise ValueError("AI discovery run is missing or no longer running")
        run_id=existing_run_id
    else:
        run_id=_start_run(conn,len(active),"ai_assisted")
    try:
        metadata={"provider":"offline_import","model":None}
        if isinstance(run_metadata,dict):
            metadata.update({k:v for k,v in run_metadata.items() if k in ("provider","model","endpoint") and isinstance(v,(str,type(None)))})
        conn.execute("UPDATE pattern_discovery_runs SET config_json=? WHERE run_id=?",
            (json.dumps({"mode":"ai_assisted","source":"current_selected_matching_input",**metadata},sort_keys=True),run_id))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # Recheck the active source set inside the persistence transaction to reject stale citations.
        current={r["candidate_id"]:r for r in selected_evidence(conn)}
        for proposal in validated:
            for item in proposal["supporting_evidence"]:
                source=current.get(item["source_candidate_id"])
                if not source or source["source_artifact_id"]!=item["source_artifact_id"] or item["evidence_excerpt"] not in (source["original_text"] or ""):
                    raise ValueError("AI citation became stale before persistence")
            draft={"proposed_text":proposal["candidate_name"],"language":proposal.get("language","und"),
              "candidate_type":proposal["candidate_category"],"description":proposal.get("description"),
              "suggested_category_key":proposal.get("suggested_category_key"),"confidence":proposal["confidence"],
              "confidence_basis":proposal["confidence_basis"],"rationale":proposal["reasoning"],
              "reasoning":proposal["reasoning"],"novelty_type":proposal["novelty_type"],
              "target_pattern_id":proposal["target_pattern_id"],"evidence":proposal["supporting_evidence"]}
            _persist_candidate(conn,run_id,draft,"ai_assisted",proposal)
        conn.execute("UPDATE pattern_discovery_runs SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE run_id=?",(run_id,))
        conn.commit()
        return {"run_id":run_id,"imported":len(validated)}
    except Exception as error:
        conn.rollback();conn.execute("UPDATE pattern_discovery_runs SET status='failed',error_text=?,completed_at=CURRENT_TIMESTAMP WHERE run_id=?",(type(error).__name__,run_id));conn.commit();raise
    finally:
        conn.close()


def _similar_candidate(conn, candidate):
    best=None
    for row in conn.execute("SELECT candidate_id,normalized_text,language FROM pattern_discovery_candidates WHERE candidate_id!=? AND discovery_status!='rejected'",(candidate["candidate_id"],)):
        if _language(row["language"])!=_language(candidate["language"]):
            continue
        score=100.0 if candidate["normalized_text"]==row["normalized_text"] else SequenceMatcher(None,candidate["normalized_text"],row["normalized_text"]).ratio()*100
        if best is None or score>best[0]: best=(score,row["candidate_id"])
    return (best[1],best[0]) if best and best[0]>=DUPLICATE_SIMILARITY_THRESHOLD else None


def _canonical_name(text, candidate_id):
    slug=re.sub(r"[^a-z0-9]+","_",normalize_phrase(text).encode("ascii","ignore").decode()).strip("_")[:48] or "pattern"
    return f"discovered_{slug}_{candidate_id[:8]}"


def review_candidate(candidate_id, decision, reviewer, note="", db_path=DB, allow_near_duplicate=False):
    """Record a human decision; only explicit approval inserts an approved pattern."""
    if decision not in ("approved","rejected","deferred"):
        raise ValueError("decision must be approved, rejected or deferred")
    if not isinstance(reviewer,str) or not reviewer.strip():
        raise ValueError("reviewer identity is required")
    conn=_connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        candidate=conn.execute("SELECT * FROM pattern_discovery_candidates WHERE candidate_id=?",(candidate_id,)).fetchone()
        if not candidate:
            raise ValueError("candidate not found")
        if candidate["discovery_status"] in ("approved","rejected"):
            raise ValueError("candidate already has a terminal review status")
        if decision=="approved" and not candidate["is_current"]:
            raise ValueError("candidate was superseded by a newer discovery run and cannot be approved")
        evidence=conn.execute("SELECT source_candidate_id,source_artifact_id,evidence_excerpt FROM pattern_discovery_candidate_evidence WHERE candidate_id=?",(candidate_id,)).fetchall()
        if not evidence:
            raise ValueError("candidate has no supporting evidence")
        current={r["candidate_id"]:r for r in selected_evidence(conn)}
        for item in evidence:
            source=current.get(item["source_candidate_id"])
            if not source or source["source_artifact_id"]!=item["source_artifact_id"] or item["evidence_excerpt"] not in (source["original_text"] or ""):
                raise ValueError("candidate contains stale or no-longer-selected evidence; rediscover before approval")
        approved_id=None
        if decision=="approved":
            novelty=candidate["novelty_type"] or "new_pattern"
            target_id=candidate["target_pattern_id"]
            if novelty in ("variant","possible_duplicate"):
                target=conn.execute("SELECT id FROM patterns WHERE id=? AND is_active=1",(target_id,)).fetchone()
                if not target:
                    raise ValueError("candidate target pattern is no longer active")
            duplicate=_find_pattern_duplicate(conn,candidate["normalized_text"],_language(candidate["language"]))
            if duplicate and duplicate[0]=="duplicate":
                raise ValueError(f"exact duplicate of approved pattern id {duplicate[1]}; cannot approve")
            if novelty=="possible_duplicate" and not allow_near_duplicate:
                raise ValueError(f"AI marked this as a possible duplicate of pattern id {target_id}; resolve it or explicitly review as distinct")
            if duplicate and not allow_near_duplicate and not (novelty=="variant" and duplicate[1]==target_id):
                raise ValueError(f"possible duplicate of approved pattern id {duplicate[1]}; resolve it or explicitly review as distinct")
            candidate_duplicate=_similar_candidate(conn,candidate)
            if candidate_duplicate and candidate_duplicate[1]==100:
                raise ValueError(f"exact duplicate of discovery candidate {candidate_duplicate[0]}; cannot approve")
            if candidate_duplicate and not allow_near_duplicate:
                raise ValueError(f"possible duplicate of discovery candidate {candidate_duplicate[0]}; resolve it or explicitly review as distinct")
            if novelty=="variant":
                approved_id=target_id
            else:
                canonical=_canonical_name(candidate["proposed_text"],candidate_id)
                cur=conn.execute("INSERT INTO patterns(canonical_name,description,is_active) VALUES (?,?,1)",
                                  (canonical,candidate["description"] or "Human-reviewed pattern discovery candidate"))
                approved_id=cur.lastrowid
                category=conn.execute("SELECT id FROM pattern_categories WHERE category_key=?",(candidate["suggested_category_key"],)).fetchone() if candidate["suggested_category_key"] else None
                if category:
                    conn.execute("INSERT INTO pattern_category_map(pattern_id,category_id) VALUES (?,?)",(approved_id,category[0]))
            conn.execute("INSERT INTO pattern_variants(pattern_id,language,variant_text) VALUES (?,?,?)",
                         (approved_id,candidate["language"],candidate["proposed_text"]))
        final_status=decision
        conn.execute("UPDATE pattern_discovery_candidates SET discovery_status=?,approved_pattern_id=?,reviewer=?,review_note=?,reviewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                     (final_status,approved_id,reviewer.strip(),note.strip(),candidate_id))
        conn.execute("INSERT INTO pattern_discovery_reviews(candidate_id,decision,reviewer,note,approved_pattern_id) VALUES (?,?,?,?,?)",
                     (candidate_id,decision,reviewer.strip(),note.strip(),approved_id))
        conn.commit()
        return {"candidate_id":candidate_id,"status":final_status,"approved_pattern_id":approved_id}
    except Exception:
        conn.rollback();raise
    finally:
        conn.close()


def main():
    parser=argparse.ArgumentParser(description="Sentinel selected-evidence pattern discovery")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("mine",help="Mine heuristic candidates from selected normalized evidence")
    export=sub.add_parser("export-ai",help="Export selected evidence for an offline AI assistant")
    export.add_argument("--output")
    imp=sub.add_parser("import-ai",help="Import a model-generated JSON suggestion file")
    imp.add_argument("path")
    sub.add_parser("ai",help="Run a configured provider adapter using selected evidence only")
    ls=sub.add_parser("list",help="List candidates with their cited source evidence")
    ls.add_argument("--status",choices=("pending_review","possible_duplicate","duplicate","deferred","approved","rejected"))
    ls.add_argument("--all",action="store_true",help="Include candidates superseded by newer discovery runs")
    show=sub.add_parser("show",help="Show a candidate and its supporting evidence")
    show.add_argument("candidate_id")
    review=sub.add_parser("review",help="Record an explicit human decision")
    review.add_argument("candidate_id");review.add_argument("decision",choices=("approved","rejected","deferred"))
    review.add_argument("--reviewer",required=True);review.add_argument("--note",default="")
    review.add_argument("--allow-near-duplicate",action="store_true")
    args=parser.parse_args()
    if args.command=="mine": result=run_local_discovery()
    elif args.command=="export-ai": result=export_ai_bundle(output_path=args.output)
    elif args.command=="import-ai": result=import_ai_suggestions(args.path)
    elif args.command=="ai":
        from pattern_discovery_ai import run_ai_discovery
        from pattern_discovery_providers import ProviderConfigurationError, ProviderRequestError
        try:
            result=run_ai_discovery()
        except (ProviderConfigurationError,ProviderRequestError,ValueError) as error:
            parser.exit(2,f"AI discovery failed: {error}\n")
    elif args.command=="list": result=list_candidates(status=args.status,include_history=args.all)
    elif args.command=="show": result=get_candidate(args.candidate_id)
    else: result=review_candidate(args.candidate_id,args.decision,args.reviewer,args.note,allow_near_duplicate=args.allow_near_duplicate)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
