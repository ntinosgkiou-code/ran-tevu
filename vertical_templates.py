"""
vertical_templates.py — Single source of truth για κάθε ειδικότητα.

Το core engine (booking + recovery + reminders + €-dashboard + back-office) είναι
HORIZONTAL: δουλεύει για κάθε ιατρείο χωρίς αλλαγή. Αυτό που αλλάζει ανά ειδικότητα:
  (1) services + τιμές, (2) recall window (winback_inactive_days) με ιατρική λογική,
  (3) message copy, (4) voice prompt (greeting/role/emergency), (5) ΦΠΑ καθεστώς.

ΦΠΑ: οι ιατρικές υπηρεσίες ιδιωτικών ιατρών ΑΠΑΛΛΑΣΣΟΝΤΑΙ ΦΠΑ (Άρθρο 22 ν.2859/2000)
→ vat_exempt=True. Οι καθαρά αισθητικές πράξεις (beauty/κομμωτήριο) έχουν 24% ΦΠΑ.

Go-to-market: πουλάμε σε υγεία (physio + γιατρούς). beauty/fitness engine-ready, not sold.
"""

# ── Κοινό universal voice guideline (όλες οι ειδικότητες το κληρονομούν) ──────
VOICE_UNIVERSAL = (
    "Μίλα σύντομα (max 2 προτάσεις), ρώτα ΕΝΑ πράγμα τη φορά, ποτέ αριθμημένες λίστες. "
    "Πάντα επιβεβαίωσε πριν κλείσεις ραντεβού. ΠΟΤΕ ιατρική συμβουλή/διάγνωση/δοσολογία — "
    "σε ιατρικό ερώτημα πες «δεν δίνω ιατρικές συμβουλές, θα το δει ο γιατρός». "
    "Μην πεις ότι είσαι AI εκτός αν ρωτηθείς ευθέως. Ευχαρίσου στο τέλος."
)

VERTICAL_TEMPLATES = {
    # ═══════════════════════════════════════════════════════ PHYSIO (#1 sales)
    "physio": {
        "label": "Φυσικοθεραπευτήριο", "type": "φυσιοθεραπευτήριο",
        "sales_focus": True, "vat_exempt": True, "package_based": True,
        "services": [
            {"name": "Πρώτη Εκτίμηση & Αξιολόγηση", "duration": 30, "price": 30},
            {"name": "Συνεδρία Φυσικοθεραπείας", "duration": 45, "price": 40},
            {"name": "Αποκατάσταση Τραυματισμού", "duration": 60, "price": 50},
            {"name": "Manual Therapy", "duration": 45, "price": 45},
            {"name": "TECAR Therapy", "duration": 30, "price": 35},
            {"name": "Αθλητική Φυσικοθεραπεία", "duration": 60, "price": 50},
        ],
        "recovery": {"winback_inactive_days": 14, "reactivation_days": 30,
                     "noshow_autoinfer_hours": 20, "package_dropoff": True},
        "copy": {
            "noshow_recovery": "Γεια σας! Χάσαμε το σημερινό σας ραντεβού φυσικοθεραπείας. Θέλετε να το ξανακλείσουμε για να συνεχίσετε την αποκατάσταση; {rebook_url}",
            "winback": "Γεια σας! Είδαμε ότι διακόψατε τις συνεδρίες στη μέση. Θέλετε να κλείσουμε συνέχεια ώστε να ολοκληρώσετε το πλάνο σας; {rebook_url}",
            "reminder": "Υπενθύμιση: ραντεβού φυσικοθεραπείας {date} στις {time}. Απαντήστε ΝΑΙ για επιβεβαίωση.",
        },
        "voice": {
            "greeting": "Καλησπέρα σας! Καλέσατε στο φυσικοθεραπευτήριο. Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Κλείνεις ραντεβού για συνεδρίες/αξιολόγηση/αποκατάσταση. Ρώτα τι ενοχλεί, προτιμώμενη μέρα/ώρα, όνομα+τηλέφωνο. Σε ερώτηση για πόνο: «θα το αξιολογήσει ο θεραπευτής στη συνεδρία».",
            "emergency_rule": "Σε οξύ τραυματισμό/έντονο πόνο μετά από ατύχημα → πες να δει γιατρό/επείγοντα, μην κλείνεις απλή συνεδρία.",
        },
        "pitch_vocab": "πακέτο 10 συνεδριών, drop-off στην 4η, παραπεμπτικό ΕΟΠΥΥ, χέρια στον ασθενή",
    },
    # ═══════════════════════════════════════════════════════ ΟΔΟΝΤΙΑΤΡΟΣ
    "dental": {
        "label": "Οδοντιατρείο", "type": "οδοντιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Πρώτη επίσκεψη & εκτίμηση", "duration": 30, "price": 30},
            {"name": "Καθαρισμός & αφαίρεση πέτρας", "duration": 45, "price": 60},
            {"name": "Σφράγισμα σύνθετης ρητίνης", "duration": 45, "price": 70},
            {"name": "Απονεύρωση (ανά ρίζα)", "duration": 75, "price": 150},
            {"name": "Εξαγωγή δοντιού (απλή)", "duration": 30, "price": 60},
            {"name": "Λεύκανση δοντιών (in-office)", "duration": 90, "price": 250},
            {"name": "Στεφάνη / Θήκη ζιρκονίας", "duration": 60, "price": 350},
        ],
        "recovery": {"winback_inactive_days": 180, "reactivation_days": 180,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Γεια σας! Χάσαμε το σημερινό σας ραντεβού στο οδοντιατρείο. Να σας κλείσουμε νέο ώστε να μην ξεμείνει το δόντι; {rebook_url}",
            "winback": "Γεια σας! Πέρασαν 6 μήνες από τον τελευταίο σας καθαρισμό & έλεγχο. Ένα ραντεβού πρόληψης τώρα γλιτώνει σφράγισμα αργότερα. {rebook_url}",
            "reminder": "Υπενθύμιση: ραντεβού στο οδοντιατρείο {date} στις {time}. Απαντήστε ΝΑΙ για επιβεβαίωση.",
        },
        "voice": {
            "greeting": "Καλημέρα σας, οδοντιατρείο, πώς μπορώ να σας εξυπηρετήσω; Θέλετε ραντεβού ή πονάει κάποιο δόντι;",
            "role_notes": "Ρώτα: πρώτη επίσκεψη ή τακτικός, τι αφορά (έλεγχος/καθαρισμός/πόνος/σφράγισμα/αισθητικό), αν πονάει πόσο. Για εμφυτεύματα/ολική αποκατάσταση κλείνεις μόνο ραντεβού εκτίμησης. Τιμές από τη λίστα.",
            "emergency_rule": "Πόνος/πρήξιμο/σπασμένο δόντι/αιμορραγία → προτεραιότητα ραντεβού ΣΗΜΕΡΑ· εκτός ωραρίου transfer στον οδοντίατρο. Εκτιναγμένο δόντι: «βάλτε το σε γάλα και ελάτε άμεσα».",
        },
        "pitch_vocab": "νεκρή βάση 6μηνου καθαρισμού, no-show από φόβο, chair-time κενά, οδοντόγραμμα, χαμένα εμφυτεύματα 1000€+",
    },
    # ═══════════════════════════════════════════════════════ ΔΕΡΜΑΤΟΛΟΓΟΣ
    "dermatology": {
        "label": "Δερματολόγος", "type": "δερματολογικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Πρώτη δερματολογική επίσκεψη", "duration": 20, "price": 50},
            {"name": "Επανεξέταση", "duration": 15, "price": 35},
            {"name": "Δερματοσκόπηση & χαρτογράφηση σπίλων", "duration": 30, "price": 80},
            {"name": "Κρυοθεραπεία (μυρμηγκιές/θηλώματα)", "duration": 20, "price": 60},
            {"name": "Βιοψία δέρματος / αφαίρεση σπίλου", "duration": 30, "price": 120},
            {"name": "Botox (ανά περιοχή)", "duration": 30, "price": 200},
            {"name": "Υαλουρονικό / fillers (ανά σύριγγα)", "duration": 40, "price": 250},
        ],
        "recovery": {"winback_inactive_days": 365, "reactivation_days": 365,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Σας περιμέναμε σήμερα αλλά δεν τα καταφέρατε. Ειδικά ο έλεγχος ελιών δεν θέλει αναβολή — κλείστε νέο ραντεβού εδώ: {rebook_url}",
            "winback": "Πάει ένας χρόνος από τον τελευταίο σας δερματολογικό έλεγχο. Ο ετήσιος έλεγχος ελιών είναι το πιο σημαντικό βήμα πρόληψης. {rebook_url}",
            "reminder": "Υπενθύμιση για το ραντεβού σας στο δερματολογικό ιατρείο {date} στις {time}. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο δερματολογικό ιατρείο. Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Ρώτα λόγο (έλεγχος ελιών/ακμή/εξάνθημα/σπίλος/αισθητική). Κλείνεις πρώτη επίσκεψη/επανεξέταση/δερματοσκόπηση/κρυοθεραπεία/αισθητική. Ξεχώρισε ιατρικά από αισθητικά (botox δεν καλύπτεται ΕΟΠΥΥ).",
            "emergency_rule": "Σπίλος που αλλάζει γρήγορα/αιμορραγεί → προτεραιότητα + flag γιατρού. Αλλεργικό shock/δύσπνοια ή ζωστήρας στο μάτι → 166/επείγοντα, μην κλείνεις ραντεβού.",
        },
        "pitch_vocab": "ασθενείς που έρχονται μια φορά και χάνονται, ετήσιος έλεγχος ελιών, no-show σε ακριβά αισθητικά, αισθητικά vs ιατρικά",
    },
    # ═══════════════════════════════════════════════════════ ΠΑΘΟΛΟΓΟΣ / ΓΕΝΙΚΟΣ
    "gp": {
        "label": "Παθολόγος / Γενικός Ιατρός", "type": "παθολογικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Παθολογική Εξέταση / Επίσκεψη", "duration": 30, "price": 50},
            {"name": "Check-up Πρόληψης", "duration": 45, "price": 80},
            {"name": "Παρακολούθηση Χρόνιου Νοσήματος", "duration": 30, "price": 40},
            {"name": "Πιστοποιητικό Υγείας / Βεβαίωση", "duration": 20, "price": 30},
            {"name": "ΗΚΓ + αξιολόγηση", "duration": 20, "price": 30},
            {"name": "Επανεξέταση / Αξιολόγηση Αποτελεσμάτων", "duration": 20, "price": 30},
            {"name": "Συνταγογράφηση / Παραπεμπτικά", "duration": 15, "price": 30},
        ],
        "recovery": {"winback_inactive_days": 180, "reactivation_days": 180,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Γεια σας! Χάσαμε το σημερινό σας ραντεβού. Η παρακολούθηση είναι σημαντική για να μη μείνετε χωρίς αγωγή/εξετάσεις. Να σας κλείσουμε νέο; {rebook_url}",
            "winback": "Γεια σας! Πέρασε ~6μηνο από την τελευταία σας επίσκεψη. Καιρός για επανεκτίμηση και ανανέωση της αγωγής σας. {rebook_url}",
            "reminder": "Υπενθύμιση: ραντεβού με τον/την ιατρό {date} στις {time}. Φέρτε τις τελευταίες εξετάσεις & φάρμακα. Απαντήστε ΝΑΙ.",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο ιατρείο του παθολόγου. Πώς μπορώ να σας βοηθήσω;",
            "role_notes": "Ρώτα λόγο (νέο πρόβλημα/παρακολούθηση χρονίου/check-up/πιστοποιητικό). Κλείνεις επίσκεψη/check-up/follow-up/ΗΚΓ. Για ανανέωση συνταγής κλείνεις σύντομο ραντεβού, δεν συνταγογραφείς.",
            "emergency_rule": "Πόνος στο στήθος/δύσπνοια/λιποθυμία/σύγχυση → ΑΜΕΣΑ 166 (ΕΚΑΒ)/επείγοντα, μην κλείνεις ραντεβού.",
        },
        "pitch_vocab": "νεκρή βάση, 6μηνο recall, χρόνιοι (πίεση/ζάχαρο/χοληστερίνη), ανανέωση συνταγής, e-συνταγογράφηση ΗΔΙΚΑ",
    },
    # ═══════════════════════════════════════════════════════ ΚΑΡΔΙΟΛΟΓΟΣ
    "cardiology": {
        "label": "Καρδιολόγος", "type": "καρδιολογικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Καρδιολογική εξέταση + ΗΚΓ", "duration": 30, "price": 60},
            {"name": "Επανεξέταση / follow-up", "duration": 20, "price": 40},
            {"name": "Triplex καρδιάς", "duration": 30, "price": 80},
            {"name": "Holter ρυθμού 24ωρο", "duration": 20, "price": 70},
            {"name": "Holter πίεσης 24ωρο", "duration": 20, "price": 60},
            {"name": "Δοκιμασία κοπώσεως", "duration": 45, "price": 90},
            {"name": "Πλήρης καρδιολογικός έλεγχος", "duration": 60, "price": 130},
        ],
        "recovery": {"winback_inactive_days": 180, "reactivation_days": 180,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Σας περιμέναμε και δεν τα καταφέρατε — κανένα πρόβλημα. Ο καρδιολογικός έλεγχος είναι σημαντικός να μην αμελείται. {rebook_url}",
            "winback": "Καλησπέρα σας! Πέρασαν 6 μήνες από τον τελευταίο καρδιολογικό σας έλεγχο. Καλό είναι να επαναλάβουμε ΗΚΓ & πίεση. {rebook_url}",
            "reminder": "Υπενθύμιση: ραντεβού στο καρδιολογικό ιατρείο {date} στις {time}. Φέρτε φάρμακα & προηγούμενες εξετάσεις. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καρδιολογικό ιατρείο, πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Κλείνεις εξέταση/triplex/Holter/test κοπώσεως/check-up. Ρώτα πρώτη φορά ή επανέλεγχος. Για test κοπώσεως: αθλητικά παπούτσια & άνετα ρούχα. Πες να φέρουν εξετάσεις & φάρμακα.",
            "emergency_rule": "Πόνος/σφίξιμο στο στήθος, δύσπνοια, λιποθυμία, παλμοί με ζάλη → ΑΜΕΣΑ 166/επείγοντα, μην κρατάς στη γραμμή.",
        },
        "pitch_vocab": "ασθενείς που έκαναν έναν έλεγχο και χάθηκαν, 6μηνο recall, χρόνιοι υπερτασικοί, no-show σε triplex/κόπωση, άυλη συνταγή",
    },
    # ═══════════════════════════════════════════════════════ ΓΥΝΑΙΚΟΛΟΓΟΣ
    "gynecology": {
        "label": "Γυναικολόγος", "type": "γυναικολογικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Γυναικολογική εξέταση (πρώτη επίσκεψη)", "duration": 30, "price": 60},
            {"name": "Επανεξέταση / follow-up", "duration": 20, "price": 40},
            {"name": "Test-Pap (κυτταρολογικό)", "duration": 20, "price": 40},
            {"name": "Διακολπικό υπερηχογράφημα", "duration": 30, "price": 60},
            {"name": "Προγεννητικός έλεγχος / υπέρηχος εγκυμοσύνης", "duration": 40, "price": 80},
            {"name": "Τοποθέτηση/αφαίρεση σπιράλ", "duration": 40, "price": 120},
            {"name": "Κολποσκόπηση", "duration": 30, "price": 80},
        ],
        "recovery": {"winback_inactive_days": 365, "reactivation_days": 365,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Καλησπέρα σας, σας περιμέναμε αλλά δεν τα καταφέρατε. Η εξέτασή σας είναι σημαντική — ας βρούμε νέα μέρα που σας βολεύει: {rebook_url}",
            "winback": "Καλησπέρα σας! Πέρασε ένας χρόνος από τον τελευταίο σας προληπτικό έλεγχο (Test-Pap & εξέταση). Είναι η ώρα για το ετήσιο check-up. {rebook_url}",
            "reminder": "Καλησπέρα σας! Υπενθύμιση για το ραντεβού σας στο γυναικολογικό ιατρείο {date} στις {time}. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο γυναικολογικό ιατρείο. Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Κλείνεις εξέταση/Test-Pap/υπέρηχο/προγεννητικό/σπιράλ. Ρώτα γενικό λόγο (έλεγχος/πρόβλημα/εγκυμοσύνη), αν έγκυος ποια εβδομάδα. ΔΙΑΚΡΙΤΙΚΟΤΗΤΑ: μη ζητάς ευαίσθητες λεπτομέρειες. Test-Pap όχι σε περίοδο.",
            "emergency_rule": "Έντονη αιμορραγία/κοιλιακός πόνος/διαρροή υγρών ή μειωμένες κινήσεις εμβρύου → άμεση αξιολόγηση: κινητό γιατρού/επείγοντα μαιευτηρίου/166.",
        },
        "pitch_vocab": "ετήσιο Test-Pap που ξεχνιέται, νεκρή βάση, no-show, διακριτικότητα, χαμένες κλήσεις στο μαιευτήριο",
    },
    # ═══════════════════════════════════════════════════════ ΠΑΙΔΙΑΤΡΟΣ
    "pediatrics": {
        "label": "Παιδίατρος", "type": "παιδιατρικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Παιδιατρική επίσκεψη / εξέταση", "duration": 30, "price": 50},
            {"name": "Επίσκεψη νεογνού", "duration": 45, "price": 70},
            {"name": "Εμβολιασμός (χορήγηση + έλεγχος)", "duration": 20, "price": 30},
            {"name": "Προληπτικός έλεγχος ανάπτυξης", "duration": 40, "price": 60},
            {"name": "Πιστοποιητικό υγείας (σχολείο/αθλητισμός)", "duration": 20, "price": 40},
            {"name": "Τηλεφωνική συμβουλευτική (follow-up)", "duration": 15, "price": 25},
            {"name": "Έκτακτη επίσκεψη αυθημερόν", "duration": 25, "price": 55},
        ],
        "recovery": {"winback_inactive_days": 60, "reactivation_days": 60,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Σας χάσαμε στο ραντεβού. Καταλαβαίνουμε ότι με τα παιδιά όλα αλλάζουν τελευταία στιγμή. Ας βρούμε νέα ώρα που σας βολεύει: {rebook_url}",
            "winback": "Γεια σας! Το παιδί σας έχει καιρό να περάσει για τον προγραμματισμένο έλεγχο/εμβόλιο. Καλό είναι να μην καθυστερήσει το σχήμα. {rebook_url}",
            "reminder": "Καλησπέρα! Υπενθύμιση για το ραντεβού του παιδιού σας {date} στις {time}. Φέρτε το βιβλιάριο υγείας. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο παιδιατρικό ιατρείο. Πώς μπορώ να βοηθήσω; Αν είναι επείγον με το παιδί, πείτε μου το αμέσως.",
            "role_notes": "Ρώτα ηλικία παιδιού (μήνες/χρόνια — κρίσιμο), λόγο (έλεγχος/εμβόλιο/άρρωστο/πιστοποιητικό). Κράτα ξεχωριστά γρήγορα slots για άρρωστα παιδιά αυθημερόν από τα προληπτικά.",
            "emergency_rule": "Βρέφος <3 μηνών με πυρετό, δύσπνοια/μπλε χείλη, σπασμοί, λήθαργος, πορφυρό εξάνθημα, κατάποση φαρμάκου → ΑΜΕΣΑ επείγοντα παιδιατρικού/166, όχι ραντεβού.",
        },
        "pitch_vocab": "χαμένα εμβόλια, πρόγραμμα εμβολιασμών, well-child visit, sick slots vs προληπτικά, γονείς που τηλεφωνούν συνέχεια",
    },
    # ═══════════════════════════════════════════════════════ ΟΡΘΟΠΕΔΙΚΟΣ
    "orthopedics": {
        "label": "Ορθοπεδικός", "type": "ορθοπεδικό ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Πρώτη ορθοπεδική επίσκεψη", "duration": 30, "price": 70},
            {"name": "Επανεξέταση / follow-up", "duration": 20, "price": 50},
            {"name": "Έγχυση υαλουρονικού (ανά άρθρωση)", "duration": 20, "price": 120},
            {"name": "Θεραπεία PRP (1 συνεδρία)", "duration": 40, "price": 180},
            {"name": "Τοποθέτηση/αλλαγή νάρθηκα ή γύψου", "duration": 25, "price": 60},
            {"name": "Διαγνωστικό υπερηχογράφημα μυοσκελετικού", "duration": 20, "price": 50},
            {"name": "Έκθεση/γνωμάτευση ΚΕΠΑ", "duration": 20, "price": 40},
        ],
        "recovery": {"winback_inactive_days": 180, "reactivation_days": 180,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Σας περιμέναμε και δεν τα καταφέρατε — κανένα πρόβλημα. Ο πόνος δεν περιμένει όμως. Ξανακλείστε σε νέα ώρα: {rebook_url}",
            "winback": "Καλησπέρα σας! Πέρασε καιρός από τον τελευταίο σας έλεγχο. Καλό είναι να επανεκτιμήσουμε την πορεία σας. {rebook_url}",
            "reminder": "Ραντεβού στο ορθοπεδικό ιατρείο: {date} στις {time}. Φέρτε προηγούμενες ακτινογραφίες/MRI αν έχετε. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο ορθοπεδικό ιατρείο. Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Ρώτα ΤΙ τον ενοχλεί (ποια άρθρωση: γόνατο/ώμος/μέση/αυχένας), πρώτη φορά ή επανεξέταση. Κλείνεις επίσκεψη/εγχύσεις/μετεγχειρητικά follow-up. Πες να φέρει ακτινογραφία/MRI.",
            "emergency_rule": "Οξύ τραυματισμό με σημάδια κατάγματος (παραμόρφωση/αδυναμία στήριξης), ανοιχτό τραύμα, απώλεια αισθητικότητας → ΑΜΕΣΑ επείγοντα/166.",
        },
        "pitch_vocab": "ασθενείς που δεν ξανάρθαν για επανέλεγχο, αναπάντητες ενώ χειρουργεί, σειρές εγχύσεων, μετεγχειρητικό follow-up, ΕΟΠΥΥ",
    },
    # ═══════════════════════════════════════════════════════ ΟΦΘΑΛΜΙΑΤΡΟΣ
    "ophthalmology": {
        "label": "Οφθαλμίατρος", "type": "οφθαλμιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Πλήρης οφθαλμολογικός έλεγχος", "duration": 30, "price": 50},
            {"name": "Μέτρηση ενδοφθάλμιας πίεσης (γλαύκωμα)", "duration": 20, "price": 40},
            {"name": "Συνταγογράφηση γυαλιών & φακών (διάθλαση)", "duration": 30, "price": 40},
            {"name": "Βυθοσκόπηση με διαστολή κόρης", "duration": 40, "price": 60},
            {"name": "OCT τομογραφία ωχράς/οπτικού νεύρου", "duration": 20, "price": 70},
            {"name": "Οπτικά πεδία (καμπιμετρία)", "duration": 30, "price": 50},
            {"name": "Παιδοφθαλμολογικός έλεγχος", "duration": 40, "price": 50},
        ],
        "recovery": {"winback_inactive_days": 365, "reactivation_days": 365,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Σας περιμέναμε και δεν τα καταφέρατε — όλα καλά! Ο έλεγχος της όρασης είναι σημαντικός, ας βρούμε νέα ώρα: {rebook_url}",
            "winback": "Γεια σας! Πέρασε πάνω από έναν χρόνος από τον τελευταίο σας οφθαλμολογικό έλεγχο. Γλαύκωμα & καταρράκτης εξελίσσονται χωρίς συμπτώματα. {rebook_url}",
            "reminder": "Υπενθύμιση: ραντεβού στο οφθαλμιατρείο {date} στις {time}. Αν γίνει διαστολή κόρης, η όραση θολώνει 2-3 ώρες — ελάτε ΧΩΡΙΣ να οδηγείτε. Αλλαγή: {rebook_url}",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο οφθαλμιατρείο. Πώς μπορώ να σας βοηθήσω;",
            "role_notes": "Ρώτα λόγο (έλεγχος/αλλαγή γυαλιών/κόκκινο μάτι/γλαύκωμα-διαβήτης/παιδί). Αν περιλαμβάνει διαστολή κόρης → προειδοποίησε «θολώνει η όραση 2-3 ώρες, μην οδηγήσετε». Φακούς επαφής να τους βγάλει πριν τη διάθλαση.",
            "emergency_rule": "Ξαφνική απώλεια όρασης, έντονος πόνος, χημικό στο μάτι, αναλαμπές με «μαύρη κουρτίνα», ξαφνική διπλωπία → επείγοντα οφθαλμολογικά/166.",
        },
        "pitch_vocab": "ασθενείς που δεν ξαναγυρνούν για ετήσιο, διαβητικοί/γλαυκωματικοί που ξεχνούν 6μηνο, χαμένες κλήσεις ενώ εξετάζει, ΕΟΠΥΥ vs ιδιωτικά",
    },
    # ═══════════════════════════════════════════ MEDICAL (generic ιατρείο alias)
    "medical": {
        "label": "Ιατρείο / Οδοντιατρείο", "type": "ιατρείο",
        "sales_focus": True, "vat_exempt": True, "package_based": False,
        "services": [
            {"name": "Επίσκεψη / Εξέταση", "duration": 30, "price": 50},
            {"name": "Έλεγχος ρουτίνας", "duration": 20, "price": 40},
            {"name": "Πρώτη επίσκεψη", "duration": 45, "price": 60},
            {"name": "Επανεξέταση", "duration": 20, "price": 30},
            {"name": "Check-up Πρόληψης", "duration": 45, "price": 80},
        ],
        "recovery": {"winback_inactive_days": 180, "reactivation_days": 180,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Γεια σας! Χάσαμε το σημερινό σας ραντεβού. Θέλετε να το ξανακλείσουμε; {rebook_url}",
            "winback": "Γεια σας! Πέρασαν 6 μήνες από τον τελευταίο σας έλεγχο. Καιρός για το επόμενο ραντεβού; {rebook_url}",
            "reminder": "Υπενθύμιση ραντεβού {date} στις {time}. Απαντήστε ΝΑΙ για επιβεβαίωση.",
        },
        "voice": {
            "greeting": "Καλησπέρα σας, καλέσατε στο ιατρείο. Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Κλείνεις επίσκεψη/έλεγχο/check-up. Ρώτα λόγο, μέρα/ώρα, όνομα+τηλέφωνο.",
            "emergency_rule": "Σε επείγον σύμπτωμα → 166/επείγοντα, μην κλείνεις ραντεβού.",
        },
        "pitch_vocab": "6μηνο recall, νεκρή βάση ασθενών, GDPR, βοηθός που σηκώνει",
    },
    # ═══════════════════════════════════════════ BEAUTY (engine-ready, ΦΠΑ 24%)
    "beauty": {
        "label": "Κέντρο Αισθητικής / Κομμωτήριο", "type": "κέντρο αισθητικής",
        "sales_focus": False, "vat_exempt": False, "package_based": False,
        "services": [
            {"name": "Καθαρισμός Προσώπου", "duration": 60, "price": 45},
            {"name": "Αποτρίχωση", "duration": 30, "price": 25},
            {"name": "Μανικιούρ", "duration": 45, "price": 20},
            {"name": "Κούρεμα & Χτένισμα", "duration": 45, "price": 25},
            {"name": "Βαφή", "duration": 90, "price": 40},
        ],
        "recovery": {"winback_inactive_days": 90, "reactivation_days": 90,
                     "noshow_autoinfer_hours": 20, "package_dropoff": False},
        "copy": {
            "noshow_recovery": "Γεια σας! Χάσαμε το ραντεβού σας σήμερα. Να σας κλείσουμε νέο; {rebook_url}",
            "winback": "Γεια σας! Έχει καιρό να σας δούμε. Να κλείσουμε ένα ραντεβού; {rebook_url}",
            "reminder": "Υπενθύμιση ραντεβού {date} στις {time}. Απαντήστε ΝΑΙ.",
        },
        "voice": {
            "greeting": "Καλησπέρα σας! Πώς μπορώ να σας εξυπηρετήσω;",
            "role_notes": "Κλείνεις ραντεβού για τις υπηρεσίες του καταλόγου. Ρώτα υπηρεσία, μέρα/ώρα, όνομα.",
            "emergency_rule": "Δεν υπάρχει ιατρικό επείγον — σε παράπονο/πρόβλημα transfer σε άνθρωπο.",
        },
        "pitch_vocab": "repeat visits, λήξη πακέτου, online booking apps",
    },
    # ═══════════════════════════════════════════ FITNESS (engine-ready, ΦΠΑ 24%)
    "fitness": {
        "label": "Γυμναστήριο / Pilates-PT Studio", "type": "γυμναστήριο",
        "sales_focus": False, "vat_exempt": False, "package_based": True,
        "services": [
            {"name": "Δοκιμαστικό μάθημα", "duration": 60, "price": 0},
            {"name": "Personal Training", "duration": 60, "price": 30},
            {"name": "Reformer Pilates", "duration": 50, "price": 20},
            {"name": "Ομαδικό μάθημα", "duration": 60, "price": 12},
            {"name": "Αξιολόγηση φυσικής κατάστασης", "duration": 45, "price": 25},
        ],
        "recovery": {"winback_inactive_days": 60, "reactivation_days": 60,
                     "noshow_autoinfer_hours": 20, "package_dropoff": True},
        "copy": {
            "noshow_recovery": "Γεια σου! Έχασες το σημερινό σου μάθημα. Να σου κλείσουμε νέο; {rebook_url}",
            "winback": "Γεια σου! Έχεις καιρό να έρθεις — να ξαναρχίσουμε; {rebook_url}",
            "reminder": "Υπενθύμιση: μάθημα {date} στις {time}. Απάντησε ΝΑΙ.",
        },
        "voice": {
            "greeting": "Γεια σου! Πώς μπορώ να σε εξυπηρετήσω;",
            "role_notes": "Κλείνεις μαθήματα/PT/αξιολόγηση. Ρώτα τι θέλει, μέρα/ώρα, όνομα.",
            "emergency_rule": "Σε τραυματισμό κατά την άσκηση → σύσταση για γιατρό.",
        },
        "pitch_vocab": "churn συνδρομών, ghost members, λήξεις, drop-in",
    },
}

# Aliases: business type → vertical key (πιο ειδικό πρώτα)
_ALIASES = {
    "φυσικοθεραπ": "physio", "φυσιοθεραπ": "physio", "physio": "physio",
    "οδοντ": "dental", "dental": "dental",
    "δερματ": "dermatology", "derma": "dermatology",
    "καρδιολ": "cardiology", "cardio": "cardiology",
    "γυναικολ": "gynecology", "μαιευτ": "gynecology", "gyn": "gynecology",
    "παιδιατ": "pediatrics", "pediatr": "pediatrics",
    "ορθοπ": "orthopedics", "ortho": "orthopedics",
    "οφθαλμ": "ophthalmology", "ophthalm": "ophthalmology",
    "παθολ": "gp", "γενικ": "gp", "gp": "gp",
    "ιατρ": "medical", "γιατρ": "medical", "medical": "medical",
    "αισθητ": "beauty", "κομμωτ": "beauty", "beauty": "beauty", "salon": "beauty", "nail": "beauty",
    "γυμναστ": "fitness", "pilates": "fitness", "gym": "fitness", "fitness": "fitness", "yoga": "fitness",
}


def resolve_vertical(business_type_or_key: str) -> str:
    """Επιστρέφει vertical key από τύπο business ή key. Default 'physio'."""
    s = (business_type_or_key or "").lower()
    if s in VERTICAL_TEMPLATES:
        return s
    for frag, key in _ALIASES.items():
        if frag in s:
            return key
    return "physio"


def get_template(business_type_or_key: str) -> dict:
    return VERTICAL_TEMPLATES[resolve_vertical(business_type_or_key)]


def recovery_copy(business_type_or_key: str, kind: str, **fmt) -> str:
    tmpl = get_template(business_type_or_key)["copy"].get(kind, "")
    try:
        return tmpl.format(**fmt)
    except (KeyError, IndexError):
        return tmpl


def winback_days(business_type_or_key: str) -> int:
    return get_template(business_type_or_key)["recovery"]["winback_inactive_days"]


def is_vat_exempt(business_type_or_key: str) -> bool:
    """True για ιατρικές υπηρεσίες (απαλλαγή ΦΠΑ Άρθρο 22 ν.2859/2000)."""
    return bool(get_template(business_type_or_key).get("vat_exempt", False))


def vat_rate_for(business_type_or_key: str) -> float:
    """ΦΠΑ %: 0 για ιατρικά (απαλλαγή), 24 για αισθητικά/beauty/fitness."""
    return 0.0 if is_vat_exempt(business_type_or_key) else 24.0


def voice_block(business_type_or_key: str) -> dict:
    """greeting/role_notes/emergency_rule για το Vapi system prompt."""
    return get_template(business_type_or_key).get("voice", {})


def list_verticals() -> list:
    """[(key, label, sales_focus)] για dropdowns/UI."""
    return [(k, v["label"], v.get("sales_focus", False)) for k, v in VERTICAL_TEMPLATES.items()]
