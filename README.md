# Cat.taxoCovX

# Extract & Inject — Couverture Taxonomique (Gemma 3 12B)

Pipeline Python en deux étapes pour **enrichir automatiquement les métadonnées de datasets** d'un portail OpenDataSoft (ODS) avec leur couverture taxonomique, en combinant un LLM local et des APIs biologiques de référence.

---

## Vue d'ensemble

```
corpus Excel  ──►  Gemma 3 12B (Ollama)  ──►  GBIF v2  ──►  JSON intermédiaire
                                                                        │
                                                                        ▼
                                              EMBL-EBI/ENA  ◄──  Injection ODS
                                              NCBI                (Automation API)
```

**Étape 1 — Extraction** (`process_pndb`) :
- Lit un fichier Excel de corpus (`mon_corpus.xlsx`)
- Soumet chaque titre + description à **Gemma 3 12B** via Ollama pour extraire les noms scientifiques d'espèces (binômes latins)
- Valide chaque nom auprès de l'**API GBIF v2** (`/species/match`)
- Sauvegarde les résultats dans un fichier JSON intermédiaire avec reprise automatique

**Étape 2 — Injection** (`inject_couverture_taxonomique`) :
- Charge le JSON intermédiaire
- Enrichit chaque espèce validée avec des liens vers **NCBI** et **EMBL-EBI/ENA** (vérifié dynamiquement)
- Génère un bloc HTML de couverture taxonomique
- Résout les `dataset_uid` en parallèle via l'**API Explore ODS**
- Injecte les métadonnées via l'**API Automation ODS** (GET → PUT → PUBLISH)

---

## Prérequis

- Python 3.9+
- [Ollama](https://ollama.com/) installé et accessible localement
- Modèle `gemma3:12b` téléchargé : `ollama pull gemma3:12b`
- Un accès à un portail **OpenDataSoft** avec une clé API Automation

### Dépendances Python

```bash
pip install pandas requests tqdm ollama python-dotenv openpyxl
```

---

## Configuration

Créez un fichier `.env` à la racine du projet :

```env
API_KEY=votre_clé_api_ods
```

Ajustez les constantes en tête de script selon votre environnement :

| Constante | Valeur par défaut | Description |
|---|---|---|
| `LOCAL_MODEL` | `gemma3:12b` | Modèle Ollama utilisé |
| `INPUT_FILE` | `mon_corpus.xlsx` | Fichier Excel source |
| `OUTPUT_FILE` | `metadata_species_pndb_local.json` | Fichier JSON intermédiaire |
| `ODS_DOMAIN_URL` | `https://www.pndb.fr` | URL de base du portail ODS |
| `MAX_WORKERS` | `10` | Threads pour la résolution parallèle des UIDs |
| `MAX_RETRIES` | `3` | Tentatives max en cas d'erreur réseau / 429 |
| `RETRY_BACKOFF` | `2` | Délai initial (s) pour le backoff exponentiel |
| `INJECT_DELAY` | `0.5` | Délai fixe (s) entre chaque injection |

---

## Format du fichier Excel source

Le script lit les colonnes suivantes dans `mon_corpus.xlsx` :

| Colonne | Description |
|---|---|
| `datasetid` | Identifiant unique du dataset |
| `default.title` | Titre du dataset (analysé par le LLM) |
| `default.description` | Description du dataset (analysée par le LLM) |

---

## Utilisation

```bash
python Extract_Inject_Couverture-taxonomique_Gemma3-12b.py
```

Le script est **interactif** et propose plusieurs choix à l'exécution :

1. **Réutiliser le JSON existant** — si `OUTPUT_FILE` est déjà présent, possibilité de sauter l'extraction
2. **Revalider les espèces non trouvées** — relance une passe GBIF sur les entrées `not_found_on_gbif`
3. **Lancer l'injection** — confirmation avant toute écriture sur le portail ODS

---

## Fichiers produits

| Fichier | Description |
|---|---|
| `metadata_species_pndb_local.json` | JSON intermédiaire indexé par `datasetid`, contenant pour chaque espèce : `scientificName`, `gbif_canonicalName`, `taxonID`, `status` |

### Exemple de structure JSON

```json
{
  "mon-dataset-id": [
    {
      "scientificName": "Equus hemionus",
      "gbif_canonicalName": "Equus hemionus",
      "taxonID": "https://www.gbif.org/species/2440897",
      "status": "validated"
    },
    {
      "scientificName": "Nomascus hainanus",
      "gbif_canonicalName": null,
      "taxonID": null,
      "status": "not_found_on_gbif"
    }
  ]
}
```

---

## Champs injectés dans ODS

Les métadonnées suivantes sont ajoutées ou mises à jour dans le bloc `custom` de chaque dataset :

| Champ ODS | Contenu |
|---|---|
| `couverture-taxonomique` | Tableau HTML avec liens GBIF, NCBI, EMBL-EBI/ENA |
| `couverture-taxonomique_en` | Idem (champ multilingue EN) |
| `couverture-taxonomique_fr` | Idem (champ multilingue FR) |
| `taxonomie` | Liste brute des noms scientifiques validés |

---

## Robustesse et reprise

- **Sauvegarde incrémentale** : le JSON est écrit tous les 10 datasets pendant l'extraction
- **Reprise automatique** : les datasets déjà présents dans le JSON sont ignorés lors d'une nouvelle exécution
- **Backoff exponentiel** : les erreurs 429 et les erreurs réseau déclenchent jusqu'à `MAX_RETRIES` tentatives
- **Résolution parallèle** : les `dataset_uid` sont résolus avec un pool de `MAX_WORKERS` threads

---

## APIs utilisées

| Service | Usage |
|---|---|
| [GBIF Species Match v2](https://www.gbif.org/developer/species) | Validation et normalisation des noms scientifiques |
| [EMBL-EBI ENA Portal API](https://www.ebi.ac.uk/ena/portal/api/) | Vérification de la présence de l'espèce dans ENA |
| NCBI Search | Lien vers les données omiques (pas d'appel API, URL directe) |
| ODS Explore API v2.1 | Résolution des `dataset_uid` |
| ODS Automation API v1.0 | Lecture et mise à jour des métadonnées |

---

## Licence

À préciser selon les conditions de votre projet.
