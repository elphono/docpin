# docpin

**Le dépôt est public** (<https://github.com/elphono/docpin>). Il s'est appelé `annotate-workflow`, puis `redmargin`
(2026-10-06), avant `docpin` : un nom qui dit ce que l'outil fait. La commande reste `annotate` et le paquet Python aussi ; le dossier local garde l'ancien
nom, parce que le renommer casserait ce qui en porte le chemin — les shebangs du `.venv`. Sur la
machine de l'auteur, l'unité systemd et le raccourci de la pastille passent par le plugin depuis le
2026-10-12, et aucun hook ne pointe plus le clone. Le README et la ROADMAP sont
la vitrine ; ce fichier est le mode d'emploi de qui modifie le code.

Successeur du pont papier de remarkable-sync (dépôt privé de l'auteur), sans tablette. **Le besoin** : quand une
session Claude Code produit un document HTML, son lecteur l'annote dans son navigateur, et les
annotations reviennent toutes seules **dans la conversation ouverte** qui l'a produit (ou, si
aucune n'attend, dans un onglet de terminal qui la reprend). Rester simple : faciliter l'échange
entre l'utilisateur et la session, via un document.

```
session Claude Code ── Write/Edit docs/x.html
        │  hook PostToolUse  hooks/register-on-write.py
        ▼
annotate register x.html --session <id> --cwd <dépôt> --hook
        │  ~/.local/share/annotate/registry.json
        │  et, à la session : « lance `annotate wait <doc>` en arrière-plan »
        ▼
session ── annotate wait <doc>  (commande d'arrière-plan : POST …/wait, tenu par le démon)
        ┊
démon `annotate serve` (127.0.0.1:8765, service systemd utilisateur)
        │  GET /docs/<id> : le HTML du disque + overlay (annotate.js)
        ▼
navigateur Windows ── Alt+clic → épingle + note ── PUT /api/docs/<id>/annotations
        │  « Send to session » (barre, pastille ou `annotate send <id>`)
        ▼
une session attend ? ── oui ─► `annotate wait` rend les notes et sort : Claude Code réveille
        │                     LA conversation ouverte, qui corrige x.html puis relance wait
        ├─ non, mais la session est OUVERTE ─► les notes sont postées dans sa boîte de
        │                     réception (socket de messagerie entre sessions) ; elle les
        │                     reçoit derrière un dialogue Approve / Deny, puis relance wait
        └─ non, et elle n'est ouverte nulle part ─► onglet Windows Terminal :
                  `claude --resume <session>` interactif (session neuve si elle a disparu)
```

**Deux voies d'entrée au registre, et il les faut toutes les deux** :

| Voie | Ce qu'elle voit | Ce qu'elle apporte |
|---|---|---|
| le hook (push) | les `Write` et `Edit` d'une session | la session à coup sûr, et la consigne `annotate wait` |
| le parcours du démon (toutes les 30 s) | tout document des dossiers suivis écrit **depuis son premier passage** | ce que Bash écrit (`cp` depuis un scratchpad, scripts, sous-agents) ; la session est retrouvée dans les transcripts |

Mesuré le 2026-10-05 : un `docs/<nom>.html` d'un dépôt du workspace, composé par un
sous-agent dans son scratchpad puis copié par `cp`, n'est jamais passé par le hook. **Rien
d'antérieur au premier passage n'est repris** (décision du 2026-10-05) : `scan.json` garde le
début du dernier passage, si bien qu'un fichier écrit démon arrêté est trouvé au démarrage
suivant, et qu'un document oublié ne revient que s'il est réécrit. `figures/` est exclu des deux
voies : ce sont les sources HTML des images d'un document.

**Les dossiers suivis se choisissent sur la page d'accueil** (décision du 2026-10-07, `folders.py`,
`~/.local/share/annotate/folders.json`). Par défaut, un seul : les `docs/` du workspace. Un dossier
ajouté — n'importe lequel sous `~` — suit **tout** `.html` en dessous, hors `build/`,
`node_modules/`, `figures/`, `.claude/`… Mesuré ce jour-là : ce qui échappait était hors de la règle
`docs/` (`~/.claude/sync/docs/`, un `rapports/` de dépôt), jamais un raté du parcours. **La règle ne
vit qu'à un endroit** : le hook passe tout `.html` à `annotate register --hook`, qui demande
`folders.wanted` ; il n'en garde aucune copie.

**Un dossier exclu l'emporte sur tout dossier suivi** (2026-10-19, liste `excluded` de
`folders.json`) : une session qui construit une application écrit des `.html` qui sont ses pages,
pas des documents, et ils entraient dans la liste. Mêmes règles qu'un dossier suivi (sous `~`,
absolu, existant) ; `folders.wanted` le consulte, donc le hook, le parcours, le rescan et
`is-document` (que le contrôle d'eli appelle) aussi, sans autre copie. Exclure un dossier passe aussitôt en
« Unmanage » les documents listés dessous (`forgotten.json`), sauf les protégés
(`registry.protection`, voir le rescan) ; retirer l'exclusion ne les ramène pas tant qu'ils ne
sont pas réécrits.

**Le rattrapage est celui de l'utilisateur, et il est borné** : « Rescan » (page, pastille,
`annotate rescan`) et l'ajout d'un dossier prennent les fichiers des N derniers jours (7 par défaut)
— un rattrapage complet aurait ramené 100 documents d'un coup. Il ne ramène pas un document passé en
« Unmanage » tant qu'il n'a pas été réécrit (`forgotten.json`, date du fichier au moment de
l'oubli). Le passage régulier, lui, ne rattrape toujours rien.

**« Rescan » fait aussi le ménage** (2026-10-19, `scanner.rescan` = `catch_up` + `prune`) : la
liste devient les documents de la fenêtre, ceux dont le fichier est plus vieux passent en
« Unmanage » (sous `--folder D` seulement, si donné), et reviennent s'ils sont réécrits. L'ajout
d'un dossier, lui, ne retire rien. **Un lot ne prend jamais un document protégé**, et la règle
n'a qu'une copie (`registry.protection`, servie à la page dans `protected`) : une session qui
écoute, le statut `delivered`, des notes non envoyées (qu'un Unmanage effacerait). **Un fichier
manquant n'est jamais protégé** (décision du 2026-10-20) : « file missing » l'emporte sur toute
autre étiquette, et les trois lots (rescan, exclusion, Cleanup files) le prennent, le rescan quelle
que soit sa fenêtre, puisqu'il n'a plus de date. La ligne de commande demande au démon (en lecture)
qui écoute ; sans démon, personne. Le rapport dit ajoutés, retirés, gardés.

**« Cleanup files » vide un groupe de la page** (`cleanup.py`, bouton à droite de l'en-tête de
chaque groupe : celui des documents sans session depuis le 2026-10-19, chaque session depuis le
2026-10-20 ; `annotate cleanup [--session ID]`) : après une confirmation dans la page qui compte
ce qui part (fichiers manquants signalés) et ce qui reste, les documents non protégés de CE groupe
passent en « Unmanage », en un lot (`POST /api/cleanup {session, ids}`, les ids montrés, filtre
actif compris ; le démon ne prend que ceux de cette session, quels que soient les ids). **Rien
n'est effacé au clic** : chaque fichier entre dans `cleanup.json` (date, taille, heure du
nettoyage) — un fichier manquant n'y entre pas, il n'y a rien à effacer —, et
le démon, au plus une fois par heure dans sa boucle de parcours, efface celui qui n'a pas bougé
depuis `CLEANUP_AFTER_DAYS` = 30 jours (`ANNOTATE_CLEANUP_AFTER_DAYS` pour les tests, la page lit
la valeur dans `/api/docs`). Un fichier touché (date ou taille) ou ré-enregistré (hook, parcours,
rescan, `register` : `registry.register` le retire de l'index) sort de l'index sans être effacé ;
un fichier hors de l'index ne l'est jamais. `annotate cleanup --list` montre les dates prévues,
`--run` applique ce qui est dû.

**La page d'accueil (`/`, ouverte d'un clic gauche sur la pastille) porte tous les contrôles
de la pastille** : par document Open, Send to session, New session, Unmanage, Delete file ; les
dossiers suivis (ajout avec suggestions, retrait), les dossiers exclus (mêmes suggestions) et
Rescan ; « Cleanup files » dans l'en-tête de chaque groupe ; pour le démon Restart et Stop, demandés à systemd (`service.control`, `--no-block`, refusés si le
démon n'a pas été lancé par systemd). **Start n'y est pas, et ne peut pas y être** : un démon
arrêté ne sert plus de page où cliquer ; il reste dans la pastille. La page passe par la même
API que la pastille, avec la même garde (`X-Annotate`, origine locale).
**Attach to session… / Detach** (2026-10-08) corrigent la session d'un document : un sélecteur
filtrable liste les conversations reprenables de la machine (`claude.sessions`, ouvertes
d'abord, au plus `SESSION_LIMIT` = 50), et `registry.attach` refuse en 400 un id que
`claude --resume` ne trouverait pas, sans toucher ni au statut ni aux notes.

**La pastille et la page d'accueil classent les documents par session** : le titre de la
session (son `/rename`, sinon celui que Claude Code a généré, lus dans le transcript, de façon
incrémentale) et son dossier ; les documents sans session connue viennent en dernier.
**La page d'accueil range en outre les sessions par dépôt** (2026-10-09) : le dépôt git qui
contient le dossier où la session tourne (`registry.repository_of`, `.git` dossier ou fichier,
jamais au-dessus du home), sinon ce dossier lui-même, présenté comme « Folder ». C'est le dossier
de la **session** qui compte, pas celui du document.
Chaque section de dépôt et chaque groupe de session **se replie** d'un clic sur son en-tête ou
son chevron (2026-10-19) ; replié, l'en-tête garde son compte. Tout est ouvert par défaut ;
l'état vit dans le `localStorage` du navigateur, une clé par dépôt et par session
(`docpin.folded.repo:<…>`, `docpin.folded.session:<id>`), et en mémoire pour la vie de la page :
sans stockage (fenêtre privée, données bloquées), le repli marche jusqu'au rechargement.

**La page met en avant ce qui a changé depuis le dernier envoi** (2026-10-12, spec
`docs/2026-10-12-mise-en-avant-des-mises-a-jour.html`). Chaque envoi copie le fichier dans
`baselines/<id>.html` du dossier de données (`registry.take_baseline`, appelé par `Sender._settle`,
seul endroit où des notes deviennent envoyées) ; la page lit la copie et le fichier par
`GET /api/docs/<id>/baseline`, les compare bloc à bloc dans le navigateur (`static/diff.js`, pur), et
`static/changes.js` marque le nouveau et le modifié, replie les sections inchangées, compte ce qu'il
ne retrouve pas. « Mark as read » (`POST …/baseline`) avance la référence. Une copie impossible à
écrire **retire** la référence : comparer à une version plus ancienne marquerait comme neuf le déjà-lu.

**Le dépôt est aussi une marketplace Claude Code** (2026-10-12, périmètre
`docs/2026-10-12-perimetre-du-plugin.html`) : un plugin, `docpin` à la racine (`.claude-plugin/`,
`hooks/hooks.json`, `bin/annotate`, le skill `skills/annotatable-documents/`). Le second, `visual-first`
(le style « visuel d'abord » et le contrôle des documents), est **passé dans eli le 2026-10-23**
(<https://github.com/elphono/eli>, marketplace à part) ; eli demande toujours à docpin ce qu'est un
document par `annotate is-document`, qui reste donc ici. Un plugin ne voit que son dossier : docpin est donc la racine, là où
vit `src/`. `bin/annotate` ne lance **jamais** le code du dossier de version, que Claude Code supprime
14 jours après une mise à jour : il le copie une fois par contenu sous `runtime/<empreinte>/` du dossier
de données (`runtime.py`), pointe `runtime/current` dessus — sauf si `current` nomme une version plus
haute — et exécute la copie avec le `python3` du système ; le démon d'une copie devient `stale` quand
`current` change. Depuis la 0.5.2, `current` est un **dossier** qui porte le lien `annotate ->
../<empreinte>/annotate` (voir « Ce qui n'est pas évident »). Il démarre le démon à la demande
(`client.ensure_daemon`, par systemd si l'unité existe) pour les commandes qui en ont besoin, jamais pour
`status`. **Chaque changement publié change la version** (`pyproject.toml`, `__init__.py`,
`.claude-plugin/plugin.json`, égales par test) : une version épinglée identique n'atteint pas les installés.

**Une conversation peut passer sur l'autre machine de l'utilisateur** (2026-10-13, `sync.py`, guide
`docs/2026-10-13-synchro-entre-machines.html`). « Send » scelle le transcript (`vault.py` : gzip, puis
`openssl enc -aes-256-cbc -pbkdf2`, puis un HMAC vérifié avant tout déchiffrement) et le dépose dans le
dossier caché de l'application sur Google Drive (`gdrive.py`, portée `drive.appdata`, client OAuth de
l'utilisateur, retour du consentement sur `/` du démon, avec PKCE et un `state` à usage unique), ou dans
un dossier qu'un autre programme synchronise. Le démon de l'autre machine regarde toutes les 60 s, dépose
la conversation là où `claude --resume` la trouve (`~/.claude/projects/<chemin où tout caractère
non alphanumérique devient un tiret>/`), dans SON clone du même dépôt, retrouvé par l'URL du remote, et propose « Resume here ». Désactivée
par défaut ; **seuls les dossiers autorisés peuvent envoyer** (liste blanche : ce poste porte aussi des
dépôts d'employeur). **Pas de concurrence à gérer**, décision du 2026-10-13 : une session n'est jamais
envoyée pendant qu'on la modifie de l'autre côté, et un envoi plus récent remplace l'ancien.

L'étude qui a fixé ces choix, avec ce qui est repris de remarkable-sync et la phase 2, est
`docs/etude-reutilisation.html`.

## Commandes

```bash
uv sync                                  # environnement (aucune dépendance d'exécution)
uv run pytest -q                         # suite par défaut
ANNOTATE_PLAYWRIGHT_DIR=<dossier dont node_modules contient playwright> \
  uv run pytest -m browser -v            # l'overlay dans un vrai Chromium headless
uv run ruff check src tests tools hooks bin/annotate plugins  # doit passer intégralement
uv run mypy                              # doit passer intégralement
uv run python tools/mutate.py            # campagne de mutation dirigée, sur une COPIE
claude plugin validate --json . > r.json && python3 tools/check_plugin_report.py r.json
claude plugin eval . --runs 1 --allow-tools Write Edit   # le skill, avec et sans plugin (payant)
claude --plugin-dir .                    # une session qui charge le clone comme plugin

uv run annotate register <chemin.html> [--session ID] [--cwd DIR]
uv run annotate wait <id>                # dans une session, EN ARRIÈRE-PLAN : rend les notes
uv run annotate list | open [<id>] | send <id> | new-session <id> | resend <id> | forget <id> [--delete]
uv run annotate sessions                 # les conversations auxquelles rattacher un document
uv run annotate attach <id> <session> [--cwd DIR] | detach <id>
uv run annotate folders [add|remove|exclude|unexclude <dossier>]   # suivis et exclus
uv run annotate rescan [--days N] [--folder D]   # rattrape les N derniers jours, retire les plus vieux
uv run annotate cleanup [--session ID|--list|--run]   # « Cleanup files », l'index d'effacement, les effacements dus
uv run annotate serve                    # le démon, au premier plan
uv run annotate service                  # écrit ~/.config/systemd/user/annotate.service
uv run annotate tray [--start|--once|--uninstall]   # pastille Windows + raccourci Démarrage
uv run annotate status                   # le démon en une ligne
uv run annotate sync [send [<session>]|poll|status]   # la synchro entre machines (send : la session courante)
```

Configuration : `~/.config/annotate/config.toml` (`port`, `claude_bin`), surchargeable par
`ANNOTATE_PORT`, `ANNOTATE_CLAUDE` ; données sous
`ANNOTATE_DATA_DIR` (défaut `~/.local/share/annotate`). `ANNOTATE_WORKSPACE` (défaut
`~/workspace`) donne le dossier suivi par défaut, tant que la liste n'a jamais été modifiée.

## Langue

**Anglais** dans tout ce qui est code : identifiants, commentaires, docstrings, noms de tests,
messages de log, libellés de l'overlay et de la pastille. **Français** pour ce fichier, les
documents de `docs/` et les messages de commit.

**Une exception, déclarée le 2026-10-06 : `README.md` et `ROADMAP.md` sont en anglais**, parce que
ce sont la vitrine d'un dépôt public. Des libellés d'interface en français seraient une autre
exception, à écrire ici avant de les écrire dans le code.

## Ce dépôt est public

Rien de ce qui appartient à un projet d'employeur n'y entre : ni nom de dépôt ou de produit, ni
chemin réel, ni nom de session, ni clé de ticket, ni hôte. Le 2026-10-06, un chemin réel de dépôt
d'employeur a été trouvé dans une docstring, un test et ce fichier ; il y avait été mis comme
exemple mesuré. Il est retiré de l'arbre, **pas de l'historique** (deux commits le portent), et
un exemple se prend désormais dans un chemin neutre (`<repo>/docs/<name>.html`). Avant d'écrire
une mesure réelle dans un test ou une docstring, la rendre anonyme.

## Signature des commits

**Aucune mention de Claude, d'une IA ou d'un co-auteur automatique** dans les messages de
commit, les descriptions de MR/PR et les commentaires de code : ni `Co-Authored-By`, ni
`Generated with`, ni emoji de robot. Cette règle prime sur les valeurs par défaut de l'outil.

## Invariants — ils sont tenus par des tests, pas par la relecture

| Invariant | Ce qui le tient |
|---|---|
| aucune dépendance d'exécution hors bibliothèque standard | `dependencies = []` dans `pyproject.toml` |
| les annotations ne vont **jamais** à côté du document (il vit dans un dépôt) | `test_annotations_never_live_next_to_the_document` |
| aucun test ne lance `claude`, `ssh`, `cmd.exe`, `powershell.exe`, `wsl.exe`, `wt.exe`, `systemctl`…, ni n'émet un vrai signal, ni ne lit les transcripts de `~/.claude` | fixtures autouse de `tests/conftest.py`, armement vérifié par `tests/test_suite_guards.py` |
| une session qui attend reçoit les notes, et aucun onglet ne s'ouvre alors | `test_an_open_session_receives_only_the_unsent_notes`, `test_the_notes_reach_the_open_session_that_waits` |
| jamais de notes livrées à une attente dont le client est parti (session fermée) | `test_a_dead_client_is_never_handed_the_notes`, `test_a_wait_whose_client_left_stops_listening` |
| un double clic ne livre les notes qu'une fois | `test_a_double_click_hands_the_notes_over_once` |
| le prompt arrive à `claude` en UN argument, quoi qu'il contienne ; aucun `;` sur la ligne de `wt.exe` | `test_the_prompt_reaches_claude_as_one_argument_in_the_right_folder`, `test_the_tab_command_never_carries_a_semicolon` |
| `send` n'envoie que les annotations jamais envoyées ; le serveur fait autorité sur `sent_at` | `test_an_open_session_receives_only_the_unsent_notes`, `test_a_stale_browser_copy_cannot_unsend_a_note` |
| une annotation dont l'ancre ne résout plus n'est jamais perdue (barre « orphelines », prompt « Anchor: lost ») | `test_a_lost_anchor_is_still_sent_with_its_quote`, `test_browser.py` |
| le parcours ne reprend rien d'antérieur à son premier passage, et ne perd rien démon arrêté | `test_nothing_older_than_the_first_pass_is_taken`, `test_the_daemon_being_down_loses_nothing` |
| l'auteur d'un document est le DERNIER appel d'outil qui le nomme avant son écriture, sous-agent compris | `test_the_writer_is_the_last_call_naming_the_file_before_it_was_written` |
| le hook n'enregistre qu'un document d'un dossier suivi, et la règle n'a qu'une copie | `test_register_for_the_hook_takes_only_a_document_of_a_tracked_folder`, `test_every_html_file_is_handed_over_and_annotate_decides` |
| un dossier ne se suit que sous `~` ; un dossier ajouté suit tout `.html`, le workspace garde sa règle `docs/` | `test_a_folder_outside_the_home_directory_or_not_absolute_is_refused`, `test_a_folder_the_user_adds_tracks_every_html_under_it` |
| rien sous un dossier exclu n'est un document, même dans un dossier suivi ou un `docs/` du workspace, pour le hook comme pour le parcours ; l'exclure passe en Unmanage ce qui est listé dessous, jamais un protégé, jamais rien à côté | `test_a_document_under_an_excluded_folder_is_never_wanted_even_in_a_tracked_one`, `test_register_for_the_hook_refuses_a_document_under_an_excluded_folder`, `test_excluding_a_folder_unmanages_the_documents_listed_under_it`, `test_excluding_a_folder_keeps_a_document_a_batch_never_takes`, `tests/browser/index_check.mjs` |
| un rescan ne prend que les N derniers jours, un à la fois, et ne ramène pas un document oublié | `test_a_catch_up_takes_the_recent_window_only`, `test_one_catch_up_at_a_time_and_the_last_one_is_reported`, `test_an_unmanaged_document_stays_out_of_a_catch_up_until_written_again` |
| un rescan retire ce qui est plus vieux que sa fenêtre (la même frontière que l'ajout), sous `--folder D` seulement sous D ; jamais un protégé (session qui écoute, `delivered`, notes non envoyées) ; un fichier manquant n'est jamais protégé et part de chaque lot, quelle que soit la fenêtre ; l'ajout d'un dossier ne retire rien | `test_a_catch_up_forgets_the_documents_older_than_its_window`, `test_a_catch_up_of_one_folder_forgets_only_under_it`, `test_a_catch_up_never_forgets_a_protected_document`, `test_a_missing_file_is_always_taken_even_with_every_protection`, `test_adding_a_folder_never_unmanages_an_older_document`, `test_a_rescan_from_the_page_keeps_the_document_a_session_waits_for` |
| un document ne se rattache qu'à une conversation que `claude --resume` trouverait ; le rattachement écrit `session_id`, sans toucher au statut ni garder un worktree disparu | `test_attaching_a_document_writes_its_session_and_moves_it_to_that_group`, `test_a_session_this_machine_cannot_resume_is_refused_with_a_400`, `test_an_attach_keeps_the_status_and_never_a_deleted_worktree_folder` |
| la liste des sessions ne propose que des conversations reprenables, jamais un sous-agent, les ouvertes d'abord, bornée sans couper une session ouverte | `test_the_list_offers_only_resumable_conversations_and_never_a_subagent`, `test_open_sessions_come_first_then_the_most_recently_active`, `test_the_list_is_bounded_and_never_cuts_an_open_session` |
| une session se range sous le dépôt qui contient son dossier (sous-dossier et worktree compris), jamais sous un dépôt du home ; les sessions d'un dépôt se regroupent, les documents sans session restent à part, en dernier | `test_each_session_group_names_the_repository_it_works_in`, `test_a_repository_in_the_home_directory_holds_no_session`, `tests/browser/index_check.mjs` |
| un dépôt ou une session se replie et garde son compte ; un nouveau rendu et un rechargement le gardent replié, le dépliage ne laisse rien dans le stockage ; sans `localStorage`, le repli marche sans erreur | `tests/browser/index_check.mjs` |
| chaque groupe a son « Cleanup files » ; il ne prend que les documents montrés de ce groupe (sans session, ou de cette session), jamais un autre groupe, jamais un protégé, et n'efface rien au clic ; un fichier manquant part toujours et n'entre pas dans l'index ; le démon n'efface qu'un fichier de l'index, jamais avant 30 jours, jamais un fichier touché (date ou taille) ni ré-enregistré, au plus une fois par heure ; une entrée illisible n'est jamais effacée ; un fichier déjà disparu sort de l'index sans erreur | `tests/test_cleanup.py`, `test_cleanup_files_from_the_page_unmanages_what_it_showed_and_deletes_nothing`, `test_cleanup_files_of_a_session_never_touches_another_session`, `test_cleanup_files_from_the_page_keeps_the_document_a_session_waits_for`, `tests/browser/index_check.mjs`, et les mutations `cleanup-*` de `tools/mutate.py` |
| la copie de référence est prise à chaque envoi, jamais à côté du document ; un envoi refusé la garde, une copie impossible la retire | `test_a_send_keeps_the_document_as_it_was_as_the_reference`, `test_a_refused_send_keeps_the_previous_reference`, `test_a_reference_that_cannot_be_written_is_removed_and_said`, `test_the_reference_never_lives_next_to_the_document` |
| référence identique au fichier → rien de dessiné ; un ajout, une modification, un retrait → une marque chacun ; une PARTIE neuve (le plus haut élément qui ne tient que des blocs neufs : liste, tableau, carte) n'en porte qu'une et rien dedans n'en reçoit (classe, étiquette, étape ▲ ▼, ligne du panneau, compteurs), un conteneur dont au moins deux enfants directs sont ou contiennent un titre se scindant en eux (grille de cartes) ; un élément neuf d'une liste ancienne, un bloc sous un bloc MODIFIÉ gardent la leur ; le fond propre d'un bloc marqué reste lisible sous la teinte ; aucune étiquette n'en couvre une autre, et celle d'un bloc est sur son bord haut, au-dessus de son texte (dans son padding s'il en a, au bout du bloc si la ligne du dessus est prise) ; une section qui porte une note non envoyée ne se replie jamais ; rien ne se replie si un changement n'a pas été retrouvé ; le repli ne déplace aucune ancre | `tests/browser/changes_check.mjs` |
| hors WSL, le navigateur et l'onglet passent par les outils de la plateforme (`xdg-open`, `open`, terminal natif, ou le `terminal` de `config.toml`), jamais par ceux de Windows ; sans workspace, les `docs/` du home sont des documents, mais le home n'est jamais parcouru | `tests/test_platforms.py` |
| aucune note n'est postée à une session ouverte sur un protocole non vérifié : elles restent à envoyer, avec la raison, et jamais d'onglet ; « Resend » ne remet que le dernier lot | `test_a_session_on_an_unchecked_protocol_gets_nothing_and_the_notes_stay`, `test_resend_puts_the_last_batch_back_and_posts_it_again` |
| le plugin n'exécute jamais le code de son dossier de version ; une copie survit à la suppression de ce dossier ; trois copies gardées, jamais celle en service ; une nouvelle version rend `stale` le démon d'une copie plus ancienne ; le raccourci Démarrage de la pastille nomme un fichier simple que chaque installation rafraîchit, jamais une copie datée ni `current` | `test_the_launcher_runs_a_copy_of_the_code_outside_the_plugin_folder`, `test_the_tray_shortcut_names_a_plain_file_that_every_install_refreshes`, `test_a_copy_survives_the_plugin_folder_being_deleted`, `test_a_new_version_gets_a_copy_of_its_own_and_three_are_kept`, `test_a_daemon_running_a_copy_turns_stale_when_a_new_version_is_installed` |
| `current` ne revient jamais à une version plus basse : un lanceur de la 0.5.1 ou d'avant (code figé, rejoué tel quel depuis `tests/legacy/`) s'arrête sur « Is a directory » sans toucher `current`, le script de la pastille ni les copies ; un lanceur neuf d'une version plus basse exécute sa copie et laisse `current` ; les versions se comparent en nombres ; jamais d'élagage de la copie que `current` nomme, et l'élagage garde les versions les plus hautes avant les plus récentes (la copie qu'un ancien lanceur fait juste avant d'échouer ne chasse pas celle d'un démon pas encore redémarré) ; le démon démarré à la demande sans unité exécute `current`, comme l'unité | `test_an_older_launcher_cannot_take_current_back`, `test_the_real_older_launcher_stops_with_an_error_and_changes_nothing`, `test_while_current_was_a_link_an_older_launcher_took_it_back` (le témoin), `test_a_lower_version_runs_its_own_copy_and_leaves_current_alone`, `test_a_higher_version_takes_current_each_time`, `test_the_copy_current_names_is_never_pruned`, `test_the_copy_current_names_is_kept_even_ranked_below_copies_of_its_version`, `test_the_copies_older_launchers_leave_go_before_the_one_a_daemon_still_runs`, `test_the_on_demand_start_runs_the_copy_current_names_not_the_launchers` |
| la consigne nomme `annotate` sans chemin seulement quand il est sur le PATH de la session (hook de plugin, ou PATH) ; un dossier `annotate/` du projet ne remplace pas le paquet | `test_the_plugin_hook_tells_the_session_to_run_annotate_by_name`, `test_a_launcher_off_the_path_is_called_back_by_its_own_path`, `test_a_project_folder_named_annotate_cannot_stand_in_for_the_package` |
| le démarrage à la demande ne vise que les commandes qui en ont besoin, et ne lance jamais un second démon | `test_the_launcher_starts_the_daemon_only_for_commands_that_need_it`, `test_a_running_daemon_is_never_started_twice` |
| les manifestes portent la version du paquet ; Claude Code valide la marketplace sans autre avertissement que ce `CLAUDE.md` | `test_every_manifest_carries_the_package_version`, job `plugin` de la CI |
| `annotate is-document` répond comme la règle des dossiers, pour un `.html` et pour un `.md` (jugé comme son jumeau HTML) : le contrôle d'eli, qui a repris celui de visual-first le 2026-10-23, s'y fie | `test_is_document_answers_as_the_folders_rule_for_html_and_markdown`, `test_register_for_the_hook_refuses_a_document_under_an_excluded_folder` |
| la synchro : rien ne part d'un dossier non autorisé ; une machine ne reçoit jamais son propre envoi ; un fichier illisible (phrase secrète fausse, abîmé) est signalé une fois et jamais retéléchargé, un stockage injoignable est réessayé ; un envoi remplace les anciens ; la page ne voit aucun secret et le fichier de réglages est en 0600 ; le stockage ne contient jamais un transcript lisible | `tests/test_sync.py` |
| la conversation reçue va dans le clone de CETTE machine : là où l'utilisateur l'a déjà déposée, sinon un dépôt où une conversation locale a tourné (jamais un transcript reçu, dont le dossier est celui de l'autre machine), sinon le workspace | `test_a_received_transcript_is_no_evidence_of_where_a_clone_lives`, `test_a_conversation_without_a_known_clone_waits_for_its_folder` |
| `/docs/<id>/files/` ne sert rien hors du dossier du document | `test_relative_files_are_served_and_nothing_outside_the_folder` |
| aucune valeur de déploiement en dur (home, distribution, IP, port hors `config.DEFAULT_PORT`) | `test_no_deployment_value_is_written_in_the_code`, avec son témoin |

Les propriétés centrales sont éprouvées par `tools/mutate.py`, qui casse chacune sur une
copie du dépôt et exige que la suite tombe. Chaque ancre doit se trouver une fois exactement : la
campagne le vérifie avant de commencer, et `tests/test_mutate.py` à chaque `pytest -q` (une ancre
périmée depuis la 0.4.0 arrêtait la campagne après une heure, le 2026-10-14).

## Ce qui n'est pas évident, et qui a coûté

- **L'unité systemd lance `.venv/bin/annotate serve` directement, jamais `uv run`** : sous
  systemd, `uv run` retransmet le SIGTERM que systemd envoie déjà à tout le groupe (mesuré dans
  remarkable-sync le 2026-09-15 : un arrêt sur trois finissait en 143 ou en SIGKILL).
- **Le serveur injecte un `<base href="/docs/<id>/files/">`** pour que les images relatives se
  chargent. Effet de bord : un lien `href="#section"` quitterait la page ; l'overlay intercepte
  les liens de fragment et pose `location.hash` lui-même.
- **Les URL données à Windows sont en `127.0.0.1`, jamais `localhost`** : avec
  `networkingMode=mirrored`, Windows essaie `localhost` en `::1` d'abord, et une socket liée à
  `127.0.0.1` dans WSL ne le reçoit pas. Mesuré le 2026-10-05 : 22 ms contre un délai expiré.
- **Toute requête qui modifie exige l'en-tête `X-Annotate`** et refuse un `Origin` étranger :
  une page web quelconque peut viser `localhost`, et `send` lance une session autorisée à
  éditer des fichiers. Le `Host` doit nommer localhost (rebinding DNS).
- **Les épingles sont mises à jour en place, jamais recréées** : les recréer à chaque
  redimensionnement faisait disparaître l'élément survolé (surlignage collé) et rendait le test
  navigateur rouge deux fois sur huit.
- **Plus aucune session en arrière-plan, et c'est une mesure.** Le premier envoi réel
  (2026-10-05) reprenait la session par `claude -p`, sans écran : 2 min 45 s, 1,71 $, l'étude
  corrigée et commitée, et rien de visible pour l'utilisateur qui attendait devant la session
  ouverte. L'envoi va désormais à la session qui attend (`annotate wait`), sinon dans un
  **onglet** Windows Terminal qui la reprend. Un onglet et non une fenêtre : `launchMode:
  fullscreen` ne vaut que pour les fenêtres neuves (remarkable-sync, 2026-09-17).
- **Une session ouverte ne reçoit jamais un onglet de plus** (2026-10-06 : « ça m'a ouvert un
  onglet avec ma session en double »). Le démon lit `~/.claude/sessions/<pid>.json` (le registre
  de `claude agents --json`) et poste dans la boîte de réception de la session (`inbox.py`).
  Le format de la ligne **n'est pas documenté** : capturé le 2026-10-06 sur un vrai
  `SendMessage`, figé par `tests/test_inbox.py`. Le démon ne revendique **aucun** `from-mode` :
  une session en `bypassPermissions` retient donc le message derrière un dialogue d'approbation,
  décision de l'utilisateur (« dialogue à chaque envoi »). Si la boîte refuse, les notes restent
  à envoyer, sans onglet. **Si elle accepte la ligne et que Claude Code l'ignore** (format changé
  par une mise à jour), le démon ne peut pas le voir : les notes comptent comme livrées et
  n'arrivent nulle part (reproduit le 2026-10-12, figé par
  `test_an_inbox_that_takes_the_line_and_ignores_it_counts_as_delivered`). Deux parades, codées le
  même jour : le démon ne poste qu'à une session dont le `peerProtocol` est un protocole sous lequel
  le format a été capturé sur un vrai `SendMessage` (2.1.289 le 2026-10-06, 2.1.296 le 2026-10-12,
  protocole 1 les deux fois) — le protocole et non la version, qui a changé sept fois en six jours ;
  et « Resend » (`annotate resend`, `Sender.resend`) remet le dernier lot à envoyer et le renvoie.
- **`wt.exe` coupe sa ligne de commande au `;`** (mesuré le 2026-10-05 : un `bash -c 'a; b'`
  n'a exécuté que la première moitié). L'onglet lance donc un script écrit sous le dossier de
  données, et le prompt passe par un fichier, jamais par la ligne de commande.
- **Le démon tourne sous systemd sans `WSL_DISTRO_NAME` ni le dossier de `wt.exe`** :
  la distribution est lue sur `wslpath -w /`, et `annotate service` met `WindowsApps` dans
  le `PATH` de l'unité (la regénérer après mise à jour).
- **`annotate` n'est pas sur le PATH des sessions** : la consigne donnée par le hook et le
  prompt portent le chemin absolu de l'entrée du virtualenv (`config.annotate_command`).
- **Un envoi qui échoue est défait, pas rejoué** : les notes ne sont marquées envoyées
  qu'une fois une session en main ; un onglet qui ne s'ouvre pas les laisse envoyables, avec
  la raison dans `last_error`. C'est l'utilisateur qui reclique.
- **Un témoin de garde doit rester inoffensif quand la garde est désarmée** : c'est exactement
  ce que fait la mutation. Le premier `test_suite_guards.py` appelait le vrai `explorer.exe` ;
  la campagne du 2026-10-05 l'a lancé pour de bon. Les témoins sont désormais des exécutables
  inertes qui portent le nom interdit.
- **La session qui répond suit les règles globales de l'utilisateur** : elle commite, et
  pousse s'il y a un remote — y compris dans un dépôt d'équipe.
- **La page se lit sur le disque à chaque requête, le Python du démon une seule fois** : après une
  mise à jour, une page neuve parle à un démon ancien. Mesuré le 2026-10-09 : le regroupement par
  dépôt, commité, dessinait de faux « Folder » parce que le démon, lancé la veille, n'envoyait aucun
  dépôt. Le démon compare désormais l'empreinte de ses `.py` à celle du démarrage (`stale` dans
  `/api/docs`) ; la page affiche alors un bandeau « Restart daemon », traite un démon qui ne dit pas
  `stale` comme périmé, et se rabat sur une liste à plat plutôt que d'inventer des groupes.
  `test_a_daemon_says_when_its_code_changed_on_disk_since_it_started`, `index_check.mjs`.
- **La page ne reçoit que des classes et `<annotate-fold>`, jamais un `div` ou un `p`** : les épingles
  s'ancrent en `nth-of-type`, qui ne compte que les frères de même balise. Un `p` inséré pour replier
  décalait l'ancre de la note suivante (mutation `fold-shifts-anchors`). Étiquettes, pastilles
  « removed » et ancien texte vivent dans la couche de l'overlay.
- **La barre s'affiche avant la fin de la comparaison** : son texte ne dit rien de l'état des
  changements. Le test du témoin lisait la page trop tôt et passait sans rien voir (mutation
  `witness-draws` survivante, 2026-10-12) ; l'overlay publie désormais `data-changes` sur
  `annotate-root` (`pending`, `none`, `shown`, `error`), et c'est là qu'on attend.
- **Une partie neuve ne porte qu'une marque** (2026-10-14, « c'est moche et trop chargé » : treize NEW sur une
  section de trois cartes, l'un sur l'autre). Un bloc neuf monte au plus haut élément qui ne tient que des blocs
  neufs (liste, tableau, `<article>`) ; **un conteneur se scinde en ses enfants directs quand au moins deux d'entre
  eux sont ou contiennent un titre `h1`–`h6`** (grille de cartes, page). La teinte est une ombre intérieure : le
  fond d'un bloc marqué (une pastille numérotée) reste dessous, lisible.
- **Le `bin/` d'un plugin est sur le PATH de l'outil Bash, pas sur celui d'un hook d'un autre plugin**
  (mesuré le 2026-10-12, sessions de test dans un home vide) : le contrôle de visual-first (passé dans eli le
  2026-10-23) ne trouvait pas `annotate` et retombait sans bruit sur la règle `docs/`. Il passe désormais par la copie stable
  (`runtime/current`), dont l'emplacement ne dépend d'aucun plugin. Les variables `CLAUDE_PLUGIN_*`
  n'arrivent pas non plus aux commandes de l'outil Bash : `bin/annotate` trouve tout par son propre
  chemin. Et `--plugin-dir` sur un plugin installé le remplace pour la session, sans doubler ses hooks.
- **Une consigne `annotate wait` sans chemin casse un montage par clone** : le hook de la config
  synchronisée passe par `bin/annotate`, mais sans plugin ce `bin/` n'est pas sur le PATH des sessions.
  Attrapé avant de servir ; le lanceur ne se nomme par son nom que si `CLAUDE_PLUGIN_ROOT` est posé ou
  si le `annotate` du PATH est lui-même.
- **Windows ne traverse pas un lien symbolique de WSL** (mesuré le 2026-10-12) : par `\\wsl.localhost`,
  `runtime/current` est un point d'analyse que `Test-Path` voit et où rien ne se lit, et `wslpath -w`
  résout le lien, si bien qu'un chemin passé par `current` devient la copie datée. La 0.3.4 avait ce
  défaut, invisible au test parce que le double de `windows_path` ne résolvait rien. Le raccourci
  Démarrage vise donc `runtime/tray.ps1`, un fichier simple que chaque installation rafraîchit.
- **Le lanceur d'une version plus ancienne reste sur le PATH des sessions ouvertes avant la mise à
  jour, et il ramenait `current` en arrière** (mesuré trois fois les 19 et 20 octobre). Claude Code garde
  14 jours les anciennes versions ; une session ouverte avant une mise à jour, et ses sous-agents, ont
  encore le `bin/annotate` de l'ancienne sur leur PATH. Un simple `annotate status` y recopiait son code
  et repointait `current` : le démon neuf passait `stale`, et le redémarrage suivant de l'unité
  (`PYTHONPATH=runtime/current`) relançait l'ancienne version. Ce code-là est figé, on ne peut pas le
  corriger : il remplace `current` par `os.replace` d'un nouveau lien, et `rename(2)` refuse de mettre un
  lien à la place d'un dossier (EISDIR). Depuis la 0.5.2, `current` est donc un dossier (le lien
  `annotate` à l'intérieur, remplacé atomiquement, et un `README.txt` qui dit pourquoi) : un lanceur
  0.3.3 à 0.5.1 s'arrête sur « error: cannot install the code under …: Is a directory » **avant** de
  toucher `current`, `tray.ps1` ou les copies, et ce que la session ouverte avant la mise à jour doit
  faire, c'est redémarrer. L'unité n'a pas changé. À partir de la 0.5.2, un lanceur neuf ne pointe jamais
  `current` vers une version plus basse que celle qu'il nomme (nombres, pas dates : 0.10.0 > 0.9.0 ; une
  version égale passe, pour un clone chargé en place), sous un verrou borné (`runtime/.lock`). Le point
  faible restant : un dossier `current` supprimé à la main laisse un ancien lanceur refaire son lien,
  jusqu'au prochain lanceur neuf. Revenir volontairement à une version ≤ 0.5.1 demande de supprimer ce
  dossier.
- **Un transcript reçu commence dans le dossier de l'AUTRE machine** (2026-10-13, attrapé par le test de
  l'aller-retour à trois trajets). `claude.cwd_of` lit le premier `cwd` : pour une session venue d'ailleurs,
  c'est un chemin de l'autre poste, qui peut exister ici (même nom d'utilisateur, même arborescence) et
  désigner un autre clone ou rien du tout. La recherche du clone écarte donc les sessions reçues, et passe
  d'abord par l'endroit où l'utilisateur a déjà déposé la session.
- **Le home n'est jamais parcouru** : mesuré le 2026-10-12, un passage sur le home d'un poste de
  développeur prend 33,6 s, contre 0,3 s pour le workspace, et le démon passe toutes les 30 s. Sans
  workspace, le dossier par défaut est donc le home en `scan: false` : le hook y prend les `docs/`,
  le parcours l'ignore (mutation `home-walked`).
- **Une correction après livraison vaut réponse** : le hook ré-enregistre le document à chaque
  écriture, et `registry.register` fait alors passer `delivered` à `answered`. La barre du
  navigateur compare de son côté la date du fichier et propose « Reload ».

## Phase 2 — étudiée, pas codée

La liste à jour de ce qui vient, de ce qui est reporté et de ce qui est refusé est `ROADMAP.md` ;
cette section en garde le raisonnement.

Surligner, entourer, barrer à la souris et y attacher une note (sélection `Range` + CSS Custom
Highlight API ; rectangle sur un `<canvas>` superposé ; même ancrage). Pas d'extension Chrome :
l'overlay injecté par le démon couvre le besoin sans installation ; une extension ne redevient
utile que pour annoter des pages que le démon ne sert pas. Détail dans
`docs/etude-reutilisation.html`.
