import json
import os
import time
import pandas as pd
import requests
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import ollama

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
# --- Extract ---
LOCAL_MODEL       = "gemma3:12b"
INPUT_FILE        = "mon_corpus.xlsx"
GBIF_API_URL      = "https://api.gbif.org/v2/species/match"

# --- Inject ---
ODS_DOMAIN_URL    = "https://www.pndb.fr"
ODS_API_KEY       = API_TOKEN = os.getenv("API_KEY")
MAX_WORKERS       = 10    # Threads parallèles pour la résolution des UIDs
MAX_RETRIES       = 3     # Tentatives max en cas de 429 / erreur réseau
RETRY_BACKOFF     = 2     # Délai initial (s), doublé à chaque tentative
INJECT_DELAY      = 0.5   # Délai fixe entre chaque injection séquentielle

# --- Shared ---
OUTPUT_FILE       = "metadata_species_pndb_local.json"


# ═════════════════════════════════════════════
# PARTIE 1 — EXTRACTION (Extract_Couverture-taxonomique.py)
# ═════════════════════════════════════════════

def extract_species_local(text):
    """Demande à Gemma 3 12B d'extraire les noms d'espèces."""
    prompt = (                                                      # ← prompt modifié en français
        "Tu es un expert en biologie. Analyse le texte suivant (en français ou en anglais) "
        "et extrais tous les noms scientifiques d'espèces (binôme latin, ex: 'Equus hemionus'). "
        "Réponds UNIQUEMENT avec un objet JSON avec la clé 'species' contenant une liste de chaînes. "
        "Si aucune espèce n'est trouvée, retourne {\"species\": []}.\n\n"
        f"Texte à analyser : \"{text}\""
    )
    try:
        response = ollama.chat(
            model=LOCAL_MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            format='json'
            # pas d'options supplémentaires nécessaires pour Gemma 3
        )
        content = json.loads(response['message']['content'])
        return content.get("species") or content.get("Species") or []
    except:
        return []

def validate_with_gbif_v2(name):
    """Valide via l'API GBIF v2 (Direct HTTP)."""
    try:
        resp = requests.get(GBIF_API_URL, params={'scientificName': name}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            diag = data.get('diagnostics', {})
            usage = data.get('usage', {})
            if diag.get('matchType') != 'NONE':
                taxon_key = usage.get('key') or data.get('usageKey')
                canonical = usage.get('canonicalName') or data.get('canonicalName')
                if taxon_key:
                    return {"taxonKey": taxon_key, "canonicalName": canonical}
    except:
        return None
    return None

def process_pndb():
    # 1. Reprise si le fichier existe
    pndb_data_mapped = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            pndb_data_mapped = json.load(f)
        print(f"✅ Reprise : {len(pndb_data_mapped)} datasets déjà traités.")

    # 2. Lecture Excel
    print(f"Chargement de {INPUT_FILE}...")
    df = pd.read_excel(INPUT_FILE)

    # 3. Boucle de traitement
    for index, row in tqdm(df.iterrows(), total=len(df), desc="Extraction"):
        dataset_id = str(row.get("datasetid"))
        
        if dataset_id in pndb_data_mapped:
            continue

        # --- CIBLAGE DES COLONNES SPECIFIQUES ---
        # On récupère le titre et la description en gérant les valeurs vides (NaN)
        title = str(row.get("default.title", "")) if pd.notna(row.get("default.title")) else ""
        description = str(row.get("default.description", "")) if pd.notna(row.get("default.description")) else ""
        
        # Fusion du texte pour l'analyse
        full_text = f"{title} {description}".strip()

        if not full_text:
            pndb_data_mapped[dataset_id] = []
            continue

        # Analyse par l'IA
        extracted_names = extract_species_local(full_text)
        
        results = []
        seen_keys = set()

        for name in extracted_names:
            name = name.strip()
            if not name: continue
            
            info = validate_with_gbif_v2(name)
            
            if info:
                t_key = info["taxonKey"]
                if t_key not in seen_keys:
                    seen_keys.add(t_key)
                    results.append({
                        "scientificName": name,
                        "gbif_canonicalName": info["canonicalName"],
                        "taxonID": f"https://www.gbif.org/species/{t_key}",
                        "status": "validated"
                    })
                    tqdm.write(f"  ✅ {name} -> {info['canonicalName']}")
            else:
                results.append({
                    "scientificName": name,
                    "gbif_canonicalName": None,
                    "taxonID": None,
                    "status": "not_found_on_gbif"
                })
                tqdm.write(f"  ❓ {name} -> Conservé (Hors GBIF)")

        pndb_data_mapped[dataset_id] = results
        
        # Sauvegarde régulière (tous les 10 datasets)
        if index % 10 == 0:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(pndb_data_mapped, f, ensure_ascii=False, indent=4)

    # Sauvegarde finale
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(pndb_data_mapped, f, ensure_ascii=False, indent=4)
    print(f"\n✨ Terminé ! Résultats dans {OUTPUT_FILE}")


# ═════════════════════════════════════════════
# PARTIE 2 — INJECTION (Extract_Inject_Couverture-taxonomique.py)
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────

def make_session() -> requests.Session:
    """Crée une session HTTP authentifiée."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Apikey {ODS_API_KEY}"})
    return session


def check_embl_ebi(scientific_name: str) -> bool:
    """
    Vérifie l'existence d'une espèce dans l'ENA via l'API Portal.
    Retourne True si l'espèce est indexée avec une description.
    """
    # On garde la casse scientifique originale (ex: Equus%20hemionus)
    query = scientific_name.replace(" ", "%20")
    
    # URL de l'API Portal (Table taxon)
    url = (
        f"https://www.ebi.ac.uk/ena/portal/api/search"
        f"?result=taxon&query=scientific_name%3D%22{query}%22&format=json"
    )
    
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # On vérifie que data est une liste non vide
            if isinstance(data, list) and len(data) > 0:
                # On valide qu'au moins un élément a une description non vide
                # (Comme dans votre test : "description": "Equus hemionus")
                if any(item.get("description") for item in data):
                    return True
    except:
        # En cas d'erreur API ou réseau, on ignore pour ne pas bloquer l'injection
        pass
        
    return False


def build_taxonomic_string(species_list: list) -> tuple:
    """
    Retourne (taxo_html_string, list_of_names).
    """
    validated = [
        sp for sp in species_list
        if sp.get("status") == "validated"
        and sp.get("scientificName")
        and sp.get("taxonID")
    ]
    if not validated:
        return None, []

    parts = []
    names_list = [] # Nouvelle liste pour stocker les noms bruts
    
    for sp in validated:
        name      = sp["scientificName"]
        names_list.append(name) # Ajout du nom scientifique à la liste
        
        taxon_url = sp["taxonID"]
        name_encoded = name.replace(" ", "%20")

        taxon_html = (
            f'<table border="0" cellpadding="0" cellspacing="0" style="display: inline-table; border-collapse: collapse;">'
            f'<tr>'
            f'<td valign="middle"><img src="/assets/theme_image/Lien_GBIF-S.png" border="0" /></td>'
            f'<td valign="middle">&#160;<a href="{taxon_url}" target="_blank">{taxon_url}</a></td>'
            f'</tr>'
            f'</table>'
        )

        ncbi_url  = f"https://www.ncbi.nlm.nih.gov/search/all/?term={name_encoded}&ac=no&sp=r"
        ncbi_html = f'<a href="{ncbi_url}" target="_blank">NCBI</a>'

        ebi_query = name_encoded
        ebi_url   = f"https://www.ebi.ac.uk/ena/browser/text-search?query={ebi_query}"
        ebi_html  = f'<a href="{ebi_url}" target="_blank">EMBL-EBI/ENA</a>'

        omics_parts = [ncbi_html]
        if check_embl_ebi(name):
            omics_parts.append(ebi_html)

        omics_html = " ; ".join(omics_parts)

        parts.append(
            f'<tr>'
            f'<td>'
            f'<strong>scientificName :</strong> {name}<br>'
            f'<strong style="vertical-align: middle;">taxonID :</strong> {taxon_html}<br>'
            f'<strong>Omics :</strong> {omics_html}'
            f'</td>'
            f'</tr>'
        )

    rows = "\n    ".join(parts)
    taxo_html_string = (
        '<table border="1" cellpadding="10" style="border-collapse: collapse; width: 100%;">'
        '\n  <tbody>\n    ' + rows + '\n  </tbody>\n</table>'
    )
    return taxo_html_string, names_list


def update_field(payload: dict, block: str, field: str, new_val) -> None:
    """
    Met à jour (ou crée) un champ dans le payload ODS Automation.
    Même logique que dans Align_dcat_creator.py.
    """
    if block not in payload:
        payload[block] = {}
    if field not in payload[block]:
        payload[block][field] = {
            "value": new_val,
            "remote_value": new_val,
            "override_remote_value": True
        }
    else:
        payload[block][field]["value"] = new_val
        payload[block][field]["override_remote_value"] = True


# ─────────────────────────────────────────────
# ÉTAPE 7 : RÉSOLUTION PARALLÈLE DES UIDs
# ─────────────────────────────────────────────

def resolve_uid(dataset_id: str) -> tuple:
    """
    Résout le dataset_uid d'un dataset via l'API Explore v2.1.
    Chaque thread crée sa propre session (thread-safe).
    Retry automatique avec backoff exponentiel en cas de 429.

    Retourne : (dataset_id, uid_or_None, error_or_None)
    """
    session = make_session()
    wait = RETRY_BACKOFF

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            url = f"{ODS_DOMAIN_URL}/api/explore/v2.1/catalog/datasets/{dataset_id}"
            res = session.get(url, timeout=10)

            if res.status_code == 429:
                if attempt < MAX_RETRIES:
                    print(f"  [429] {dataset_id} — attente {wait}s "
                          f"(tentative {attempt}/{MAX_RETRIES})")
                    time.sleep(wait)
                    wait *= 2
                    continue
                else:
                    return dataset_id, None, f"429 après {MAX_RETRIES} tentatives"

            res.raise_for_status()
            uid = res.json().get("dataset_uid")
            if uid:
                return dataset_id, uid, None
            else:
                return dataset_id, None, "dataset_uid introuvable dans la réponse"

        except requests.exceptions.HTTPError as e:
            return dataset_id, None, str(e)
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  [ERR] {dataset_id} — attente {wait}s "
                      f"(tentative {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                wait *= 2
            else:
                return dataset_id, None, str(e)

    return dataset_id, None, "Échec après toutes les tentatives"


def resolve_uids_parallel(dataset_ids: list) -> dict:
    """
    Résout en parallèle (MAX_WORKERS threads) les dataset_uid.
    Retourne un dict { dataset_id: dataset_uid }.
    """
    total = len(dataset_ids)
    print(f"\n{'─'*60}")
    print(f"ÉTAPE 7 — Résolution parallèle de {total} dataset_uid "
          f"({MAX_WORKERS} workers, {MAX_RETRIES} tentatives max)")
    print(f"{'─'*60}")

    uid_map = {}
    skipped = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(resolve_uid, did): did
            for did in dataset_ids
        }
        resolved = 0
        for future in as_completed(futures):
            dataset_id, uid, error = future.result()
            resolved += 1
            if uid:
                uid_map[dataset_id] = uid
                print(f"  [{resolved}/{total}] ✅ {dataset_id} → {uid}")
            else:
                skipped.append(dataset_id)
                print(f"  [{resolved}/{total}] ⏭  SKIP {dataset_id} : {error}")

    if skipped:
        print(f"\n  ⚠️  {len(skipped)} dataset(s) non résolu(s) (ignorés à l'injection) :")
        for did in skipped:
            print(f"    - {did}")

    return uid_map


# ─────────────────────────────────────────────
# ÉTAPE 8 : INJECTION SÉQUENTIELLE (GET → PUT → PUBLISH)
# ─────────────────────────────────────────────

def inject_taxonomy(rows_to_inject: list, uid_map: dict) -> None:
    """
    Injecte la couverture taxonomique pour chaque dataset via l'API Automation.
    Flux par dataset : GET metadata → modifier custom block → PUT → PUBLISH.

    Les champs ciblés dans le bloc 'custom' :
      - couverture-taxonomique
      - couverture-taxonomique_en
      - couverture-taxonomique_fr
    """
    total = len(rows_to_inject)
    print(f"\n{'─'*60}")
    print(f"ÉTAPE 8 — Injection séquentielle ({total} datasets)")
    print(f"{'─'*60}")

    session = make_session()
    success_count = 0

    for r in tqdm(rows_to_inject, desc="Injection", unit="dataset"):
        dataset_id   = r["dataset_id"]
        taxo_string  = r["taxonomic_string"]
        taxo_list    = r["taxonomic_list"] # Récupération de la liste
        dataset_uid  = uid_map.get(dataset_id)

        if not dataset_uid:
            tqdm.write(f"  [SKIP] dataset_uid non résolu pour {dataset_id}")
            continue

        tqdm.write(f"\n[*] {dataset_id}")
        tqdm.write(f"    → {taxo_string[:80]}{'…' if len(taxo_string) > 80 else ''}")

        try:
            # ── A. GET : récupération du JSON complet via Automation ──────────
            url_meta = (f"{ODS_DOMAIN_URL}/api/automation/v1.0/"
                        f"datasets/{dataset_uid}/metadata/")
            res_get = session.get(url_meta)
            res_get.raise_for_status()
            payload = res_get.json()

            # ── B. Nettoyage préalable du bloc 'custom' ───────────────────────
            # Si au moins une des clés de couverture taxonomique existe déjà,
            # on supprime les trois pour éviter les doublons et ne pas dégrader
            # les autres clés du bloc custom.
            TAXO_KEYS = (
                "couverture-taxonomique",
                "couverture-taxonomique_en",
                "couverture-taxonomique_fr",
            )
            custom_block = payload.get("custom", {})
            if any(k in custom_block for k in TAXO_KEYS):
                for k in TAXO_KEYS:
                    custom_block.pop(k, None)
                tqdm.write("    🧹 Clés couverture-taxonomique existantes supprimées.")

            # ── C. Modification du bloc 'custom' ──────────────────────────────
            for field_name in TAXO_KEYS:
                update_field(payload, "custom", field_name, taxo_string)
                update_field(payload, "custom", "taxonomie", taxo_list)

            # ── D. PUT : renvoi du JSON complet modifié ───────────────────────
            res_put = session.put(url_meta, json=payload)
            if res_put.status_code != 200:
                tqdm.write(f"    [ERREUR PUT] {res_put.status_code} : {res_put.text}")
                continue

            # ── D. PUBLISH : publication des métadonnées ──────────────────────
            url_pub = (f"{ODS_DOMAIN_URL}/api/automation/v1.0/"
                       f"datasets/{dataset_uid}/publish_metadata/")
            res_pub = session.post(url_pub)
            if res_pub.status_code not in (200, 204):
                tqdm.write(f"    [ERREUR PUBLISH] {res_pub.status_code} : {res_pub.text}")
                continue

            tqdm.write("    ✅ Mis à jour et publié.")
            success_count += 1

        except Exception as e:
            tqdm.write(f"    [ERREUR] {dataset_id} : {e}")

        time.sleep(INJECT_DELAY)

    print(f"\n✨ Terminé ! {success_count}/{total} datasets mis à jour.")


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

def revalidate_not_found(output_file: str, species_data: dict) -> None:
    """
    Revalide via l'API GBIF tous les enregistrements dont le statut est
    'not_found_on_gbif'. Si le nom scientifique est désormais reconnu,
    les champs 'gbif_canonicalName', 'taxonID' et 'status' sont mis à jour.
    Le fichier JSON est sauvegardé après la revalidation.
    """
    # Collecter tous les enregistrements à revalider
    candidates = []
    for dataset_id, species_list in species_data.items():
        for sp in species_list:
            if sp.get("status") == "not_found_on_gbif":
                candidates.append((dataset_id, sp))

    total = len(candidates)
    if total == 0:
        print("  ℹ️  Aucun enregistrement 'not_found_on_gbif' à revalider.")
        return

    print(f"\n  → {total} nom(s) scientifique(s) à revalider via GBIF…")
    updated = 0

    for dataset_id, sp in tqdm(candidates, desc="Revalidation GBIF", unit="nom"):
        name = sp.get("scientificName", "").strip()
        if not name:
            continue

        info = validate_with_gbif_v2(name)
        if info:
            t_key = info["taxonKey"]
            sp["gbif_canonicalName"] = info["canonicalName"]
            sp["taxonID"] = f"https://www.gbif.org/species/{t_key}"
            sp["status"] = "validated"
            updated += 1
            tqdm.write(f"  ✅ {name} → {info['canonicalName']} (validé)")
        else:
            tqdm.write(f"  ❓ {name} → toujours introuvable sur GBIF (statut inchangé)")

    # Sauvegarde du fichier mis à jour
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(species_data, f, ensure_ascii=False, indent=4)

    print(f"\n  ✨ Revalidation terminée : {updated}/{total} nom(s) mis à jour dans {output_file}.")


def inject_couverture_taxonomique():
    # ── 1. Chargement du fichier d'entrée ─────────────────────────────────────
    if not os.path.exists(OUTPUT_FILE):
        print(f"❌ Fichier introuvable : {OUTPUT_FILE}")
        return

    print(f"Chargement de {OUTPUT_FILE}…")
    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        species_data = json.load(f)

    print(f"  → {len(species_data)} datasets chargés.")

    # ── 2. Filtrage : uniquement les datasets avec ≥1 espèce validée ──────────
    rows_to_inject = []
    skipped_empty  = 0

    for dataset_id, species_list in species_data.items():
        if not species_list:
            skipped_empty += 1
            continue

        taxo_string, taxo_list = build_taxonomic_string(species_list) # Récupération des deux éléments
        if not taxo_string:
            skipped_empty += 1
            continue

        rows_to_inject.append({
            "dataset_id":      dataset_id,
            "taxonomic_string": taxo_string,
            "taxonomic_list":   taxo_list # On stocke la liste ici
        })

    print(f"\n  → {len(rows_to_inject)} datasets à injecter "
          f"({skipped_empty} ignorés : aucune espèce validée).")

    if not rows_to_inject:
        print("Aucun dataset à injecter. Fin du script.")
        return

    # ── 3. Aperçu avant confirmation ──────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Aperçu des injections prévues :")
    print(f"{'─'*60}")
    for r in rows_to_inject[:10]:   # Afficher les 10 premiers
        print(f"  • {r['dataset_id']}")
        print(f"      {r['taxonomic_string'][:100]}{'…' if len(r['taxonomic_string']) > 100 else ''}")
    if len(rows_to_inject) > 10:
        print(f"  … et {len(rows_to_inject) - 10} autres.")

    print(f"\n{'='*60}")
    answer_revalid = input("Lancer la résolution des taxonID ? [o/N] : ").strip().lower()
    if answer_revalid == "o":
        revalidate_not_found(OUTPUT_FILE, species_data)
        # Reconstruire rows_to_inject avec les espèces nouvellement validées
        rows_to_inject = []
        skipped_empty  = 0
        for dataset_id, species_list in species_data.items():
            if not species_list:
                skipped_empty += 1
                continue
            taxo_string, taxo_list = build_taxonomic_string(species_list)
            if not taxo_string:
                skipped_empty += 1
                continue
            rows_to_inject.append({
                "dataset_id":       dataset_id,
                "taxonomic_string": taxo_string,
                "taxonomic_list":   taxo_list
            })
        print(f"\n  -> Apres revalidation : {len(rows_to_inject)} datasets a injecter "
              f"({skipped_empty} ignores : aucune espece validee).")

    answer = input("Lancer la résolution des UIDs puis l'injection ? [o/N] : ").strip().lower()
    if answer != "o":
        print("Injection annulée.")
        return

    # ── 4. Étape 7 : résolution parallèle des UIDs ────────────────────────────
    dataset_ids = [r["dataset_id"] for r in rows_to_inject]
    uid_map = resolve_uids_parallel(dataset_ids)

    if not uid_map:
        print("❌ Aucun UID résolu. Injection impossible.")
        return

    # ── 5. Étape 8 : injection séquentielle ───────────────────────────────────
    inject_taxonomy(rows_to_inject, uid_map)


# ═════════════════════════════════════════════
# POINT D'ENTRÉE
# ═════════════════════════════════════════════

if __name__ == "__main__":
    # ── Choix : réutiliser le fichier existant ou tout recalculer ─────────────
    if os.path.exists(OUTPUT_FILE):
        print(f"\n{'═'*60}")
        print(f"Le fichier '{OUTPUT_FILE}' existe déjà.")
        answer_reuse = input("Réutiliser ce fichier sans recalculer ? [o/N] : ").strip().lower()
    else:
        answer_reuse = "n"

    if answer_reuse != "o":
        # ── ÉTAPE 1-6 : Extraction des espèces → OUTPUT_FILE ─────────────────
        print(f"\n{'═'*60}")
        print("ÉTAPES 1-6 — Extraction des espèces (LLM + GBIF)")
        print(f"{'═'*60}")
        process_pndb()
    else:
        print(f"\n⏭  Extraction ignorée — utilisation de '{OUTPUT_FILE}' existant.")

    # ── ÉTAPES 7-8 : Injection dans ODS ──────────────────────────────────────
    print(f"\n{'═'*60}")
    print("ÉTAPES 7-8 — Injection de la couverture taxonomique (ODS)")
    print(f"{'═'*60}")
    inject_couverture_taxonomique()