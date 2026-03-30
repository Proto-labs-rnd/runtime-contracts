#!/bin/bash
# verify-session.sh — Vérifier la cohérence d'une session R&D
# Usage: ./tools/verify-session.sh

set -euo pipefail

echo "=== 🔍 Vérification de Session R&D ==="
echo

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 1. Runtime metadata
echo "1️⃣  Runtime Metadata"
echo "   Session ID: ${SESSION_ID:-inconnu}"
echo "   Status: ${SESSION_STATUS:-inconnu}"
echo "   Durée: ${SESSION_RUNTIME_MS:-inconnu} ms"
echo

# 2. Fichiers modifiés (dernières 24h)
echo "2️⃣  Fichiers Modifiés (24h)"
echo "   Workspace: /mnt/shared-storage/openclaw/workspace-labs"
echo

if [ -d "/mnt/shared-storage/openclaw/workspace-labs" ]; then
    find /mnt/shared-storage/openclaw/workspace-labs -type f -mtime -1 -ls 2>/dev/null | head -20 || echo "   Aucun fichier récent trouvé"
else
    echo "   ${RED}Erreur: Workspace non accessible${NC}"
fi
echo

# 3. Mémoire de session du jour
echo "3️⃣  Mémoire de Session"
TODAY=$(date +%Y-%m-%d)
MEMORY_FILE="/mnt/shared-storage/openclaw/workspace-labs/memory/${TODAY}.md"

if [ -f "$MEMORY_FILE" ]; then
    echo "   ${GREEN}✓${NC} Fichier trouvé: $MEMORY_FILE"
    echo "   Sujet annoncé:"
    head -20 "$MEMORY_FILE" | grep -E "## 🎯|## 📋|### [0-9]" || echo "   Impossible d'extraire le sujet"
else
    echo "   ${YELLOW}⚠${NC} Aucune mémoire trouvée pour aujourd'hui"
fi
echo

# 4. Vérification de cohérence
echo "4️⃣  Cohérence"
echo

# Vérifier si runtime dit "failed" mais fichiers existent
if [ "${SESSION_STATUS:-}" = "failed" ]; then
    # Compter les fichiers modifiés aujourd'hui
    FILES_CHANGED=$(find /mnt/shared-storage/openclaw/workspace-labs -type f -mtime -1 2>/dev/null | wc -l)

    if [ "$FILES_CHANGED" -gt 0 ]; then
        echo "   ${YELLOW}⚠ INHCOHRENCE DTECTÉE${NC}"
        echo "   Runtime dit: failed"
        echo "   Mais $FILES_CHANGED fichiers ont été modifiés"
        echo "   → Investigation nécessaire"
    else
        echo "   ${GREEN}✓${NC} Cohérent (failed + aucun fichier modifié)"
    fi
else
    echo "   ${GREEN}✓${NC} Runtime OK, pas d'incohérence détectée"
fi
echo

# 5. Recommandation
echo "5️⃣  Recommandation"
echo

if [ "${SESSION_STATUS:-}" = "failed" ] && [ -f "$MEMORY_FILE" ]; then
    echo "   ${YELLOW}⚠ Action requise:${NC}"
    echo "   1. Relire $MEMORY_FILE"
    echo "   2. Vérifier que le travail décrit correspond aux fichiers"
    echo "   3. Corriger si nécessaire"
elif [ ! -f "$MEMORY_FILE" ]; then
    echo "   ${YELLOW}⚠ Action requise:${NC}"
    echo "   Créer $MEMORY_FILE pour documenter cette session"
else
    echo "   ${GREEN}✓${NC} Tout semble cohérent"
fi
echo

echo "=== 🏁 Fin de Vérification ==="
