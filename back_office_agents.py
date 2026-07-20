"""
back_office_agents.py — Back-Office Agent Registry & Observability

Κάθε λειτουργία που σήμερα γίνεται χειροκίνητα στο laptop της γραμματείας
μοντελοποιείται ως autonomous agent. Σε αντίθεση με τους front-desk agents
(clinic_agents.py) που μιλάνε με τον ασθενή, αυτοί δουλεύουν ΜΕΣΑ στο σύστημα:
διαχειρίζονται φάκελο, οικονομικά/myDATA, αποθήκη, επικοινωνία — και συνδέονται
μεταξύ τους "σαν Obsidian" μέσω κοινού data layer (back_office.py:bo_links).
"""
from datetime import datetime, timedelta
from database import _conn

BACK_OFFICE_AGENTS = [
    {
        "id": "crm_agent",
        "name": "Agent Ψηφιακού Φακέλου (CRM)",
        "role": "Φτιάχνει & ενημερώνει αυτόματα φάκελο για κάθε ασθενή: στοιχεία, ιστορικό επισκέψεων, σημειώσεις, έγγραφα, tags. Linkάρει κάθε ραντεβού/τιμολόγιο/επικοινωνία στον φάκελο.",
        "replaces": "Το χειροκίνητο γράψιμο καρτέλας ασθενή & ψάξιμο 'πότε ήρθε τελευταία;'",
        "trigger": "event (κάθε νέο ραντεβού) + nightly sync",
        "connectors": ["bo_patients", "bookings", "bo_links (graph)"],
        "executor": "back_office.py:sync_patients_from_bookings",
        "_kind": "crm",
    },
    {
        "id": "calendar_optimizer",
        "name": "Agent Βελτιστοποίησης Ημερολογίου",
        "role": "Υπολογίζει chair-time utilization, εντοπίζει κενά slots, χρωματικό coding ανά υπηρεσία, προτείνει γέμισμα κενών από waitlist/recall.",
        "replaces": "Το 'κοιτάω το ημερολόγιο να δω πού έχω τρύπα' & τα μισοάδεια απογεύματα",
        "trigger": "real-time (on view) + daily 08:00",
        "connectors": ["bookings", "waitlist", "businesses (ωράριο)"],
        "executor": "back_office.py:calendar_utilization",
        "_kind": "calendar",
    },
    {
        "id": "finance_agent",
        "name": "Agent Οικονομικών & myDATA",
        "role": "Εκδίδει αυτόματα τιμολόγιο (net+ΦΠΑ) για κάθε ολοκληρωμένο ραντεβού, παρακολουθεί υπόλοιπα/χρεωστικά, καταγράφει έξοδα, και ετοιμάζει τα στοιχεία τιμολόγησης έτοιμα για τον λογιστή/πάροχο (η υποβολή στην ΑΑΔΕ γίνεται από εκείνους).",
        "replaces": "Τη χειροκίνητη καθημερινή τιμολόγηση & το 'ποιος μου χρωστάει'",
        "trigger": "event (ραντεβού completed) + daily",
        "connectors": ["bo_invoices", "bo_payments", "bo_expenses"],
        "executor": "back_office.py:autoinvoice_completed_bookings + outstanding_balances",
        "_kind": "finance",
    },
    {
        "id": "comm_hub",
        "name": "Agent Επικοινωνίας (Hub)",
        "role": "Ενοποιεί SMS/email/κλήσεις/Viber σε ένα timeline ανά ασθενή. Προγραμματίζει recall (vertical-aware: physio 14μ, ιατρείο 180μ). Κάθε επικοινωνία linkάρεται στον φάκελο.",
        "replaces": "Το σκόρπιο 'του έστειλα μήνυμα; τον πήρα τηλέφωνο;' σε 3 διαφορετικά apps",
        "trigger": "event (κάθε μήνυμα/κλήση) + weekly recall",
        "connectors": ["bo_comm_log", "Resend", "Twilio", "Vapi"],
        "executor": "back_office.py:log_comm + recall_due",
        "_kind": "comm",
    },
    {
        "id": "inventory_agent",
        "name": "Agent Αποθήκης & Προμηθευτών",
        "role": "Παρακολουθεί απόθεμα αναλώσιμων, ειδοποιεί όταν χρειάζεται παραγγελία (qty ≤ reorder level), κρατά λίστα προμηθευτών & ιστορικό παραγγελιών.",
        "replaces": "Το 'τέλειωσαν τα γάντια/βελόνες και το κατάλαβα όταν τα χρειάστηκα'",
        "trigger": "event (ανάλωση) + daily reorder check",
        "connectors": ["bo_inventory", "bo_suppliers", "bo_supplier_orders"],
        "executor": "back_office.py:low_stock",
        "_kind": "inventory",
    },
    {
        "id": "graph_linker",
        "name": "Agent Διασύνδεσης (Obsidian Graph)",
        "role": "Συντηρεί τις διασυνδέσεις μεταξύ όλων: ασθενής ↔ ραντεβού ↔ τιμολόγιο ↔ επικοινωνία ↔ έγγραφο ↔ ανάλωσιμο. Κάθε στοιχείο έχει backlinks — ό,τι ανοίξεις, βλέπεις τα πάντα γύρω του.",
        "replaces": "Το ανθρώπινο 'πρέπει να θυμάμαι ότι αυτό συνδέεται με εκείνο'",
        "trigger": "event (σε κάθε create) — αυτόματο",
        "connectors": ["bo_links (knowledge graph)"],
        "executor": "back_office.py:link / backlinks",
        "_kind": "graph",
    },
    {
        "id": "office_manager",
        "name": "Agent Office Manager (To-Do)",
        "role": "Συνθέτει αυτόματα το 'τι πρέπει να γίνει σήμερα' από όλους τους agents: ποιους ασθενείς να καλέσεις (recall), τι να παραγγείλεις, ποια χρεωστικά να κυνηγήσεις — με προτεραιότητα.",
        "replaces": "Τον office manager που κανένα μικρό ιατρείο δεν έχει",
        "trigger": "daily 08:00 + on-demand",
        "connectors": ["bo_tasks", "όλοι οι παραπάνω agents"],
        "executor": "back_office.py:rebuild_tasks",
        "_kind": "tasks",
    },
]


def _q(c, sql, params, default=0):
    try:
        r = c.execute(sql, params).fetchone()
        return (r[0] if r else default) or default
    except Exception:
        return default


def get_back_office_status(slug: str, days: int = 30) -> list:
    """Live status + δραστηριότητα ανά back-office agent (ground truth από DB)."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    with _conn() as c:
        for a in BACK_OFFICE_AGENTS:
            k, activity, metric = a["_kind"], 0, ""
            if k == "crm":
                activity = _q(c, "SELECT COUNT(*) FROM bo_patients WHERE slug=?", (slug,))
                metric = f"{activity} φάκελοι ασθενών"
            elif k == "calendar":
                activity = _q(c, "SELECT COUNT(*) FROM bookings WHERE slug=? AND created>=?", (slug, since))
                metric = "live chair-time tracking"
            elif k == "finance":
                activity = _q(c, "SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND created>=?", (slug, since))
                metric = f"{activity} τιμολόγια εκδόθηκαν"
            elif k == "comm":
                activity = _q(c, "SELECT COUNT(*) FROM bo_comm_log WHERE slug=? AND created>=?", (slug, since))
                metric = f"{activity} επικοινωνίες logged"
            elif k == "inventory":
                activity = _q(c, "SELECT COUNT(*) FROM bo_inventory WHERE slug=?", (slug,))
                metric = f"{activity} είδη σε παρακολούθηση"
            elif k == "graph":
                activity = _q(c, "SELECT COUNT(*) FROM bo_links WHERE slug=?", (slug,))
                metric = f"{activity} διασυνδέσεις"
            elif k == "tasks":
                activity = _q(c, "SELECT COUNT(*) FROM bo_tasks WHERE slug=? AND status='open'", (slug,))
                metric = f"{activity} ανοιχτές εργασίες"
            out.append({
                "id": a["id"], "name": a["name"], "role": a["role"], "replaces": a["replaces"],
                "trigger": a["trigger"], "connectors": a["connectors"], "executor": a["executor"],
                "status": "active", "activity_30d": activity, "metric": metric,
            })
    return out


def back_office_agents_summary(slug: str) -> dict:
    agents = get_back_office_status(slug)
    return {"total_agents": len(agents), "active": len(agents), "agents": agents}
