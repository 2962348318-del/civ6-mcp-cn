"""Diplomacy domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL, _bail, _bail_lua, _lua_close_diplo_session
from civ_mcp.lua.models import (
    AgendaInfo,
    CivInfo,
    DealItem,
    DealOptions,
    DiplomacyModifier,
    DiplomacySession,
    PendingDeal,
    TestTradeItem,
    TestTradeResult,
    TradeableCity,
    VisibleCity,
)


def build_diplomacy_query() -> str:
    """Rich diplomacy query — runs in InGame context for GetDiplomaticAI access."""
    return """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local states = {"ALLIED","DECLARED_FRIEND","FRIENDLY","NEUTRAL","UNFRIENDLY","DENOUNCED","WAR"}
local checkActions = {"DIPLOACTION_DIPLOMATIC_DELEGATION","DIPLOACTION_DECLARE_FRIENDSHIP","DIPLOACTION_DENOUNCE","DIPLOACTION_RESIDENT_EMBASSY","DIPLOACTION_OPEN_BORDERS","DIPLOACTION_MAKE_ALLIANCE"}
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() then
        local cfg = PlayerConfigurations[i]
        local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
        local leaderName = Locale.Lookup(cfg:GetLeaderName())
        local met = pDiplo:HasMet(i) and "1" or "0"
        local war = pDiplo:IsAtWarWith(i) and "1" or "0"
        if pDiplo:HasMet(i) then
            local ai = Players[i]:GetDiplomaticAI()
            local stateIdx = ai:GetDiplomaticStateIndex(me)
            local stateName = states[stateIdx + 1] or tostring(stateIdx)
            -- Grievances are Gathering Storm only — nil on Standard/R&F rulesets
            local grievances = 0
            pcall(function() grievances = pDiplo:GetGrievancesAgainst(i) end)
            local vis = 0
            pcall(function() vis = pDiplo:GetVisibilityOn(i) end)
            local hasDel = pDiplo:HasDelegationAt(i) and "1" or "0"
            local hasEmb = pDiplo:HasEmbassyAt(i) and "1" or "0"
            local theyDel = Players[i]:GetDiplomacy():HasDelegationAt(me) and "1" or "0"
            local theyEmb = Players[i]:GetDiplomacy():HasEmbassyAt(me) and "1" or "0"
            print("CIV|" .. i .. "|" .. civName .. "|" .. leaderName .. "|" .. met .. "|" .. war .. "|" .. stateName .. "|" .. grievances .. "|" .. vis .. "|" .. hasDel .. "|" .. hasEmb .. "|" .. theyDel .. "|" .. theyEmb)
            -- LEADER_* 类型名(审查 F2-8):doctrine 的"领袖 → 历史议程"
            -- 直查靠它;此前只在议程块内部用,从没发出来,下游恒为空。
            pcall(function() print("LEADERTYPE|" .. i .. "|" .. tostring(cfg:GetLeaderTypeName())) end)
            local okMil, milStr = pcall(function() return Players[i]:GetStats():GetMilitaryStrength() end)
            local okMyMil, myMilStr = pcall(function() return Players[me]:GetStats():GetMilitaryStrength() end)
            if okMil and okMyMil then print("MILITARY|" .. i .. "|" .. (milStr or 0) .. "|" .. (myMilStr or 0)) end
            local nCivCities = 0
            -- 统治胜利只认【原始首都】,所以目标选择必须知道哪座城是首都。
            -- 城市对象上没有 IsCapital/IsOriginalCapital(2026-08-11 实测,
            -- 两个 pcall 都拿不到),可用的只有 GetCities():GetCapitalCity()
            -- 与 city:GetOriginalOwner()。两者相交才是"还没失守的原始首都" ——
            -- 即打下来能让统治计数 +1 的那一座。
            local capX, capY = -1, -1
            pcall(function()
                local capCity = Players[i]:GetCities():GetCapitalCity()
                if capCity then capX, capY = capCity:GetX(), capCity:GetY() end
            end)
            for _, ec in Players[i]:GetCities():Members() do
                nCivCities = nCivCities + 1
                local ecx, ecy = ec:GetX(), ec:GetY()
                if pVis:IsRevealed(ecx, ecy) then
                    local ecName = Locale.Lookup(ec:GetName())
                    local ecPop = ec:GetPopulation()
                    local ecLoy, ecLoyPT = 100, 0
                    pcall(function()
                        local ecCult = ec:GetCulturalIdentity()
                        if ecCult then ecLoy = ecCult:GetLoyalty(); ecLoyPT = ecCult:GetLoyaltyPerTurn() end
                    end)
                    local ecWalls, ecDef = 0, 0
                    pcall(function()
                        for _, d in ec:GetDistricts():Members() do
                            local di = GameInfo.Districts[d:GetType()]
                            if di and di.DistrictType == "DISTRICT_CITY_CENTER" then
                                ecWalls = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                ecDef = ec:GetStrengthValue() or 0
                                break
                            end
                        end
                    end)
                    local ecOrigCap = "0"
                    pcall(function()
                        if ecx == capX and ecy == capY and ec:GetOriginalOwner() == i then
                            ecOrigCap = "1"
                        end
                    end)
                    print("ECITY|" .. i .. "|" .. ecName:gsub("|","/") .. "|" .. ecx .. "," .. ecy .. "|" .. ecPop .. "|" .. string.format("%.0f|%.1f", ecLoy, ecLoyPT) .. "|" .. ecWalls .. "|" .. ecDef .. "|" .. ecOrigCap)
                end
            end
            print("CIVCITIES|" .. i .. "|" .. nCivCities)
            local mods = nil
            pcall(function() mods = ai:GetDiplomaticModifiers(me) end)
            if mods then
                for _, mod in ipairs(mods) do
                    local txt = tostring(mod.Text):gsub("|", "/")
                    print("MOD|" .. i .. "|" .. mod.Score .. "|" .. txt)
                end
            end
            if stateIdx == 0 then
                local ok3, aType = pcall(function() return pDiplo:GetAllianceType(i) end)
                if ok3 and aType and aType >= 0 then
                    local aNames = {"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}
                    local aLevel = 1
                    pcall(function() aLevel = pDiplo:GetAllianceLevel(i) or 1 end)
                    print("ALLIANCE|" .. i .. "|" .. (aNames[aType+1] or tostring(aType)) .. "|" .. aLevel)
                end
            end
            local avail = {}
            for _, aName in ipairs(checkActions) do
                local ok2, valid = pcall(function() return pDiplo:IsDiplomaticActionValid(aName, i, false) end)
                if ok2 and valid then
                    local label = aName:gsub("DIPLOACTION_", "")
                    if label == "OPEN_BORDERS" then label = "Open Borders (via propose_trade)" end
                    table.insert(avail, label)
                end
            end
            if not pDiplo:IsAtWarWith(i) then
                local canWar = false
                pcall(function() canWar = pDiplo:CanDeclareWarOn(i) end)
                if canWar then table.insert(avail, "DECLARE_WAR") end
            end
            if #avail > 0 then print("ACTIONS|" .. i .. "|" .. table.concat(avail, ",")) end
            -- Agendas (visibility-gated: historical always, random only at SECRET+)
            local okAg, agendas = pcall(function() return Players[i]:GetAgendaTypes() end)
            if okAg and agendas then
                local histSet = {}
                local leaderType = cfg:GetLeaderTypeName()
                for ha in GameInfo.HistoricalAgendas() do
                    if ha.LeaderType == leaderType then
                        local aDef = GameInfo.Agendas[ha.AgendaType]
                        if aDef then histSet[aDef.Index] = true end
                    end
                end
                local vis = 0
                pcall(function() vis = pDiplo:GetVisibilityOn(i) end)
                for _, agIdx in ipairs(agendas) do
                    local aDef = GameInfo.Agendas[agIdx]
                    if aDef then
                        local isHist = histSet[agIdx] or false
                        if isHist then
                            print("AGENDA|" .. i .. "|HISTORICAL|" .. Locale.Lookup(aDef.Name) .. "|" .. Locale.Lookup(aDef.Description))
                        elseif vis >= 3 then
                            print("AGENDA|" .. i .. "|HIDDEN|" .. Locale.Lookup(aDef.Name) .. "|" .. Locale.Lookup(aDef.Description))
                        else
                            print("AGENDA|" .. i .. "|HIDDEN|???|Requires Secret diplomatic visibility (spy or alliance)")
                        end
                    end
                end
            end
            local okPact, hasPact = pcall(function() return Players[i]:GetDiplomacy():HasDefensivePact(me) end)
            if okPact and hasPact then print("PACT|" .. i .. "|DEFENSIVE") end
        else
            print("CIV|" .. i .. "|Unmet Civilization|Unknown Leader|" .. met .. "|" .. war .. "|UNKNOWN|0|0|0|0|0|0")
        end
    end
end
-- Scan for third-party defensive pacts
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        for j = i+1, 62 do
            if j ~= me and Players[j] and Players[j]:IsAlive() and Players[j]:IsMajor() and pDiplo:HasMet(j) then
                local okP, hp = pcall(function() return Players[i]:GetDiplomacy():HasDefensivePact(j) end)
                if okP and hp then print("PACT|" .. i .. "|" .. j .. "|DEFENSIVE") end
            end
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_diplomacy_session_query() -> str:
    """Check for open diplomacy sessions and return choices (InGame context).

    Also reads the DiplomacyActionView UI controls to capture the leader's
    actual dialogue text, reason/agenda subtext, and visible button labels.
    Button info helps detect goodbye phase (only "Goodbye" button visible).
    """
    return f"""
local me = Game.GetLocalPlayer()
local found = false
local dialogueText = ""
local reasonText = ""
local ctrl1 = ContextPtr:LookUpControl("/InGame/DiplomacyActionView/LeaderResponseText")
local ctrl2 = ContextPtr:LookUpControl("/InGame/DiplomacyActionView/LeaderReasonText")
if ctrl1 then local ok, t = pcall(ctrl1.GetText, ctrl1); if ok and t and t ~= "" then dialogueText = t end end
if ctrl2 then local ok, t = pcall(ctrl2.GetText, ctrl2); if ok and t and t ~= "" then reasonText = t end end
local dealCtrl = ContextPtr:LookUpControl("/InGame/DiplomacyDealView/LeaderResponseText")
if dealCtrl and not dealCtrl:IsHidden() then local ok, t = pcall(dealCtrl.GetText, dealCtrl); if ok and t and t ~= "" then dialogueText = t end end
local btnTexts = {{}}
for _, path in ipairs({{
    "/InGame/DiplomacyActionView/SelectionStack/Selection1Button/SelectionText",
    "/InGame/DiplomacyActionView/SelectionStack/Selection2Button/SelectionText",
}}) do
    local btn = ContextPtr:LookUpControl(path)
    if btn then
        local par = btn:GetParent()
        if par and not par:IsHidden() then
            local ok, t = pcall(btn.GetText, btn)
            if ok and t and t ~= "" then btnTexts[#btnTexts + 1] = t end
        end
    end
end
local goodbyeBtn = ContextPtr:LookUpControl("/InGame/DiplomacyActionView/GoodbyeButton")
if goodbyeBtn and not goodbyeBtn:IsHidden() then btnTexts[#btnTexts + 1] = "GOODBYE" end
local buttonInfo = table.concat(btnTexts, ";")
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() then
        local sid = DiplomacyManager.FindOpenSessionID(me, i)
        if sid and sid >= 0 then
            local cfg = PlayerConfigurations[i]
            local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local leaderName = Locale.Lookup(cfg:GetLeaderName())
            local atWar = Players[me]:GetDiplomacy():IsAtWarWith(i) and "1" or "0"
            print("SESSION|" .. sid .. "|" .. i .. "|" .. civName .. "|" .. leaderName .. "|" .. dialogueText .. "|" .. reasonText .. "|" .. buttonInfo .. "|" .. atWar)
            found = true
            local okD, deal = pcall(function() return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, i) end)
            if okD and deal then
                local okN, cnt = pcall(function() return deal:GetItemCount() end)
                if okN and cnt and cnt > 0 then
                    for item in deal:Items() do
                        local fromTag = "THEM"
                        local okF, fromID = pcall(function() return item:GetFromPlayerID() end)
                        if okF and fromID == me then fromTag = "US" end
                        local itype = item:GetType()
                        local typeName = "UNKNOWN"
                        local itemName = "Unknown"
                        local ok3, vid = pcall(function() return item:GetValueTypeID() end); vid = (ok3 and vid) and tostring(vid) or ""
                        local ok4, sid2 = pcall(function() return item:GetSubTypeID() end); sid2 = (ok4 and sid2) and tostring(sid2) or ""
                        local amount = item:GetAmount() or 0
                        local duration = item:GetDuration() or 0
                        if itype == DealItemTypes.AGREEMENTS then
                            typeName = "AGREEMENT"
                            if vid == "OPEN_BORDERS" or sid2 == "DIPLOACTION_OPEN_BORDERS" then itemName = "Open Borders"
                            elseif vid == "JOINT_WAR" or sid2 == "DIPLOACTION_JOINT_WAR" then itemName = "Joint War"
                            elseif string.find(vid, "ALLIANCE") or sid2 == "DIPLOACTION_ALLIANCE" then
                                local aMap = {{RESEARCH="Research", CULTURAL="Cultural", ECONOMIC="Economic", MILITARY="Military", RELIGIOUS="Religious"}}
                                local aKey = string.match(vid, "ALLIANCE_(%w+)")
                                itemName = ((aKey and aMap[aKey]) or aKey or "Unknown") .. " Alliance"
                            elseif vid == "MAKE_PEACE" or sid2 == "DIPLOACTION_MAKE_PEACE"
                                    or sid2 == "MAKE_PEACE" then
                                -- 2026-08-04 P0:阿拉伯求和曾因映射缺失被静默丢弃,
                                -- 简报完全不显示(交接文档 P0 MAKE_PEACE)。显形。
                                itemName = "Peace Treaty"
                            else
                                -- 未知协议类型必须显形,不许设空被过滤。
                                itemName = "Agreement#" .. (vid ~= "" and vid or sid2)
                            end
                        elseif itype == DealItemTypes.GOLD then
                            typeName = "GOLD"
                            itemName = duration > 0 and "Gold per turn" or "Gold (lump)"
                        elseif itype == DealItemTypes.RESOURCES then
                            typeName = "RESOURCE"
                            itemName = vid ~= "" and vid or "Resource"
                        elseif itype == DealItemTypes.FAVOR then
                            typeName = "FAVOR"; itemName = "Diplomatic Favor"
                        elseif itype == DealItemTypes.CITIES then
                            typeName = "CITY"; itemName = "City"
                        elseif itype == DealItemTypes.GREATWORK then
                            typeName = "GREAT_WORK"; itemName = "Great Work"
                        end
                        if itemName ~= "" and itemName ~= "Unknown" then
                            print("DEAL_ITEM|" .. i .. "|" .. fromTag .. "|" .. typeName .. "|" .. itemName:gsub("|","/") .. "|" .. amount .. "|" .. duration)
                        end
                    end
                end
            end
        end
    end
end
if not found then print("NONE") end
print("{SENTINEL}")
"""


def build_diplomacy_choices_query(other_player_id: int) -> str:
    """Get available dialogue choices for an open session with a specific player."""
    return f"""
local me = Game.GetLocalPlayer()
local sid = DiplomacyManager.FindOpenSessionID(me, {other_player_id})
if sid == nil or sid < 0 then {_bail("ERR:NO_SESSION")} end
print("SESSION|" .. sid)
local ctrl = ContextPtr:LookUpControl("/InGame/DiplomacyActionView")
local isVisible = ctrl and not ctrl:IsHidden() or false
print("VISIBLE|" .. tostring(isVisible))
for row in GameInfo.DiplomacySelections() do
    if string.find(row.Type, "FIRST_MEET") or string.find(row.Type, "GREETING") or string.find(row.Type, "DECLARE_FRIEND") or string.find(row.Type, "DENOUNCE") then
        local text = Locale.Lookup(row.Text)
        print("CHOICE|" .. row.Type .. "|" .. row.Key .. "|" .. text)
    end
end
print("{SENTINEL}")
"""


def build_diplomacy_respond(other_player_id: int, response: str) -> str:
    """Respond to a diplomacy session.

    response is 'POSITIVE', 'NEGATIVE', or 'EXIT'.
    EXIT closes the session directly (last-resort for orphaned sessions).
    POSITIVE/NEGATIVE sends AddResponse only — does NOT call CloseSession.
    The C++ engine handles session lifecycle through its own callbacks.
    Caller must check session state in a SEPARATE call to allow the engine
    time to process the response (same-frame checks see stale state).
    """
    return f"""
local me = Game.GetLocalPlayer()
local sid = DiplomacyManager.FindOpenSessionID(me, {other_player_id})
if sid == nil or sid < 0 then {_bail("ERR:NO_SESSION")} end
if "{response}" == "EXIT" then
    DiplomacyManager.CloseSession(sid)
    LuaEvents.DiplomacyActionView_ShowIngameUI()
    pcall(function() Events.HideLeaderScreen() end)
    print("OK:SESSION_CLOSED")
    print("{SENTINEL}"); return
end
DiplomacyManager.AddResponse(sid, me, "{response}")
print("OK:RESPONSE_SENT|{response}")
print("{SENTINEL}")
"""


def build_check_diplomacy_session_state(other_player_id: int) -> str:
    """Check if a diplomacy session is still open after AddResponse.

    Must be called in a SEPARATE TCP round-trip from AddResponse to give
    the C++ engine time to process the response and transition/close
    the session naturally.  Returns SESSION_OPEN or SESSION_CLOSED.
    """
    return f"""
local me = Game.GetLocalPlayer()
local sid = DiplomacyManager.FindOpenSessionID(me, {other_player_id})
if sid and sid >= 0 then
    print("SESSION_OPEN|" .. sid)
else
    pcall(function() LuaEvents.DiplomacyActionView_ShowIngameUI() end)
    pcall(function() Events.HideLeaderScreen() end)
    print("SESSION_CLOSED")
end
print("{SENTINEL}")
"""


def build_send_diplo_action(other_player_id: int, action_name: str) -> str:
    """Send a proactive diplomatic action and detect acceptance/rejection.

    action_name is e.g. DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE,
    RESIDENT_EMBASSY, DECLARE_SURPRISE_WAR, DECLARE_FORMAL_WAR, etc.

    Open Borders is NOT supported here — it's a trade deal, not a diplomatic
    action. Use propose_trade with AGREEMENT/OPEN_BORDERS items instead.

    Key discovery: RequestSession uses DIFFERENT action strings from DIPLOACTION_ names:
    - DECLARE_FRIENDSHIP -> session string "DECLARE_FRIEND" (not "DECLARE_FRIENDSHIP")
    - Others use same name as action_name

    Flow: RequestSession -> 2x AddResponse(POSITIVE) -> CloseSession
    No AddStatement needed (that crashes on mismatched session types).
    """
    # Map action_name to the correct RequestSession string
    # Game source: DiplomacyActionView.lua line 472 uses "DECLARE_FRIEND"
    session_string_map = {
        "DECLARE_FRIENDSHIP": "DECLARE_FRIEND",
        "DIPLOMATIC_DELEGATION": "DIPLOMATIC_DELEGATION",
        "RESIDENT_EMBASSY": "RESIDENT_EMBASSY",
        "DENOUNCE": "DENOUNCE",
        # War declarations — session strings match action names
        "DECLARE_SURPRISE_WAR": "DECLARE_SURPRISE_WAR",
        "DECLARE_FORMAL_WAR": "DECLARE_FORMAL_WAR",
        "DECLARE_HOLY_WAR": "DECLARE_HOLY_WAR",
        "DECLARE_LIBERATION_WAR": "DECLARE_LIBERATION_WAR",
        "DECLARE_RECONQUEST_WAR": "DECLARE_RECONQUEST_WAR",
        "DECLARE_PROTECTORATE_WAR": "DECLARE_PROTECTORATE_WAR",
        "DECLARE_COLONIAL_WAR": "DECLARE_COLONIAL_WAR",
        "DECLARE_TERRITORIAL_WAR": "DECLARE_TERRITORIAL_WAR",
    }
    is_war = action_name.endswith("_WAR") and action_name.startswith("DECLARE_")
    session_str = session_string_map.get(action_name, action_name)

    # War declarations use CanDeclareWarOn; other actions use IsDiplomaticActionValid
    if is_war:
        validation_block = f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local target = {other_player_id}
local action = "{action_name}"
if pDiplo:IsAtWarWith(target) then
    {_bail("ERR:ALREADY_AT_WAR|Already at war with this player")}
end
local canWar = false
pcall(function() canWar = pDiplo:CanDeclareWarOn(target) end)
if not canWar then
    {_bail("ERR:CANNOT_DECLARE_WAR|Cannot declare war. Possible reasons: friendship/alliance active, 10-turn peace cooldown, or target is invalid.")}
end"""
    else:
        validation_block = f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local target = {other_player_id}
local action = "{action_name}"
local fullAction = "DIPLOACTION_" .. action
local valid, results = pDiplo:IsDiplomaticActionValid(fullAction, target, true)
if not valid then
    local reasons = "unknown"
    if results and results.FailureReasons then
        local parts = {{}}
        for _, r in ipairs(results.FailureReasons) do
            local s = tostring(r or "")
            if s:find("OBSOLETE_CIVIC") or s:find("ObsoleteCivic") then
                table.insert(parts, "obsolete (Diplomatic Service civic researched — use embassy instead)")
            else
                local loc = Locale.Lookup(s)
                if loc and loc ~= "" then table.insert(parts, loc) else table.insert(parts, s) end
            end
        end
        if #parts > 0 then reasons = table.concat(parts, "; ") end
    end
    if reasons == "unknown" and action == "DIPLOMATIC_DELEGATION" then
        local dipSvcCivic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
        if dipSvcCivic then
            local hasCivic = false
            pcall(function() hasCivic = Players[me]:GetCulture():HasCivic(dipSvcCivic.Index) end)
            if hasCivic then
                reasons = "obsolete (Diplomatic Service civic researched — use embassy instead)"
            end
        end
    end
    {_bail_lua('"ERR:INVALID|" .. reasons')}
end"""

    # War declarations open the session ASYNCHRONOUSLY (same-frame
    # FindOpenSessionID returns -1, measured 2026-08-04). The old flow tried
    # to AddResponse+verify in the same frame, then left the session open for
    # an 8s background task that never runs under mcp_call.py (fresh process
    # per call, so asyncio.create_task dies with it). Stale sessions wedged
    # DiplomacyActionView and caused AI-turn CPU-pegged loops (5x measured).
    # New flow: Phase 1 requests the session only; Python sleeps a frame then
    # runs build_war_confirm() to respond, verify, and close synchronously.
    close_session_lua = "" if is_war else "    DiplomacyManager.CloseSession(sid)"
    cleanup_lua = "" if is_war else _lua_close_diplo_session()

    return f"""
{validation_block}
-- Clean stale session for THIS target only (not all session IDs).
-- Mass-closing sessions via IsSessionIDOpen loop corrupts AI diplomacy state.
local staleSid = DiplomacyManager.FindOpenSessionID(me, target)
if staleSid and staleSid >= 0 then
    DiplomacyManager.CloseSession(staleSid)
end
-- Open session with the correct action string
local isWar = action:find("_WAR") ~= nil
DiplomacyManager.RequestSession(me, target, "{session_str}")
local sessionCompleted = false
if not isWar then
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if sid and sid >= 0 then
        DiplomacyManager.AddResponse(sid, me, "POSITIVE")
        DiplomacyManager.AddResponse(sid, me, "POSITIVE")
{close_session_lua}
        sessionCompleted = true
    end
else
    -- War: session opens on a later frame; Python confirms via
    -- build_war_confirm(). Report requested so callers don't misread.
    local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
    print("OK:WAR_REQUESTED|" .. action .. " requested on " .. name)
    print("{SENTINEL}")
    return ""
end
{cleanup_lua}
-- Report result.
-- NOTE: All post-state queries (HasDelegationAt, HasEmbassyAt, IsDiplomaticActionValid,
-- GetGoldBalance, GetVisibilityOn) are STALE same-frame after CloseSession. The C++ engine
-- commits state changes on the next frame. So we cannot reliably detect acceptance by
-- comparing pre/post state. Instead: IsDiplomaticActionValid passed the pre-check (line above),
-- meaning the action was valid. If the session completed, the game accepted it.
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
if action == "DIPLOMATIC_DELEGATION" then
    if sessionCompleted then
        print("OK:ACCEPTED|" .. name .. " accepted your delegation")
    else
        print("OK:ACCEPTED|Delegation sent to " .. name)
    end
elseif action == "RESIDENT_EMBASSY" then
    print("OK:ACCEPTED|" .. name .. " accepted your embassy")
elseif action == "DECLARE_FRIENDSHIP" then
    print("OK:ACCEPTED|" .. name .. " accepted your friendship declaration")
elseif action == "DENOUNCE" then
    print("OK:SENT|Denounced " .. name)
else
    print("OK:SENT|" .. action .. " sent to " .. name)
end
print("{SENTINEL}")
"""


def build_war_respond(other_player_id: int) -> str:
    """Phase 2a/2b: send ONE POSITIVE response to the war session.

    Responses must be spaced across frames — sending two AddResponse calls
    back-to-back in one Lua execution gets swallowed (measured 2026-08-04:
    two-in-one-call left the war state uncommitted, while frame-spaced
    responses from the manual run_lua workflow always committed). Call this
    twice with a ~1s sleep between.
    """
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if sid and sid >= 0 then
    DiplomacyManager.AddResponse(sid, me, "POSITIVE")
    print("OK:RESPONDED|" .. sid)
else
    print("ERR:NO_SESSION|no open war session for target " .. target)
end
print("{SENTINEL}")
"""


def build_war_verify_close(other_player_id: int) -> str:
    """Phase 2c: verify war state, then close the session and restore UI.

    Must run AFTER the two frame-spaced responses. Closes synchronously so no
    stale session can wedge the AI turn.
    """
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local atWar = false
pcall(function() atWar = Players[me]:GetDiplomacy():IsAtWarWith(target) end)
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
if atWar then
    print("OK:WAR_DECLARED|" .. name)
else
    print("WARN:WAR_UNCERTAIN|war state not yet confirmed for " .. name)
end
for r = 1, 5 do
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if not sid or sid < 0 then break end
    DiplomacyManager.CloseSession(sid)
end
LuaEvents.DiplomacyActionView_ShowIngameUI()
pcall(function() Events.HideLeaderScreen() end)
print("{SENTINEL}")
"""


def build_cleanup_stale_sessions() -> str:
    """Force-close any open diplomacy session and restore the UI.

    Defensive sweep run before end_turn: stale sessions (war declarations or
    peace deals whose cleanup died with the calling process) leave
    DiplomacyActionView stuck, which blocks RequestSession and can wedge the
    AI turn (measured 5x CPU-pegged infinite loops, all following diplomatic
    actions). Closing every open session is safe: any session the AI cares
    about re-opens on its own turn.
    """
    return f"""
local me = Game.GetLocalPlayer()
local closed = 0
for i = 0, 62 do
    if Players[i] and Players[i]:IsAlive() and i ~= me then
        for r = 1, 5 do
            local sid = DiplomacyManager.FindOpenSessionID(me, i)
            if not sid or sid < 0 then break end
            DiplomacyManager.CloseSession(sid)
            closed = closed + 1
        end
    end
end
if closed > 0 then print("CLOSED_STALE_SESSIONS|" .. closed) end
LuaEvents.DiplomacyActionView_ShowIngameUI()
pcall(function() Events.HideLeaderScreen() end)
print("OK:STALE_SESSIONS_CLEANED")
print("{SENTINEL}")
"""


def build_war_close_session(other_player_id: int) -> str:
    """Phase 1: close the war diplomacy session (InGame context).

    After this, DiplomacyActionView transitions to OVERVIEW_MODE (intel screen).
    Must wait ~1s for the engine event to process before phase 2.
    """
    sentinel = SENTINEL
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if sid and sid >= 0 then
    DiplomacyManager.CloseSession(sid)
end
print("OK:SESSION_CLOSED")
print("{sentinel}")
"""


def build_war_dismiss_view() -> str:
    """Phase 2: force-dismiss DiplomacyActionView from OVERVIEW_MODE (InGame context).

    NaturalWonderPopup_Shown triggers OnBlockingPopupShown → OnForceClose →
    CloseFocusedState(true) → Close() which calls UninitializeView + ShowIngameUI
    (balancing InitializeView's HideIngameUI).  NaturalWonderPopup_Closed then
    balances NW's own BulkHide(true) entry.

    Must be called in a SEPARATE Lua execution from CloseSession — the engine
    fires OnDiplomacySessionClosed asynchronously, so the view needs a frame to
    transition from CONVERSATION_MODE to OVERVIEW_MODE first.
    """
    return """
LuaEvents.NaturalWonderPopup_Shown()
LuaEvents.NaturalWonderPopup_Closed()
pcall(function() Events.HideLeaderScreen() end)
print("OK:VIEW_DISMISSED")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_deal_options_query(other_player_id: int) -> str:
    """Show what both sides can trade — resources, gold, favor, agreements (InGame)."""
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not Players[target] or not Players[target]:IsAlive() then {_bail(f"ERR:INVALID_PLAYER|Player {other_player_id} not found")} end
if not pDiplo:HasMet(target) then {_bail(f"ERR:NOT_MET|Have not met player {other_player_id}")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("CIV|" .. target .. "|" .. name:gsub("|","/"))
local ourGold = math.floor(Players[me]:GetTreasury():GetGoldBalance())
local ourGPT = math.floor(Players[me]:GetTreasury():GetGoldYield() - Players[me]:GetTreasury():GetTotalMaintenance())
local ourFavor = 0
pcall(function() ourFavor = math.floor(Players[me]:GetFavor() or 0) end)
local theirGold = math.floor(Players[target]:GetTreasury():GetGoldBalance())
local theirGPT = math.floor(Players[target]:GetTreasury():GetGoldYield() - Players[target]:GetTreasury():GetTotalMaintenance())
local theirFavor = 0
pcall(function() theirFavor = math.floor(Players[target]:GetFavor() or 0) end)
print("ECON|" .. ourGold .. "|" .. ourGPT .. "|" .. ourFavor .. "|" .. theirGold .. "|" .. theirGPT .. "|" .. theirFavor)
for row in GameInfo.Resources() do
    local ourAmt = Players[me]:GetResources():GetResourceAmount(row.Index)
    local theirAmt = Players[target]:GetResources():GetResourceAmount(row.Index)
    if ourAmt > 0 or theirAmt > 0 then
        local rClass = row.ResourceClassType or ""
        local rName = Locale.Lookup(row.Name)
        print("RES|" .. rName:gsub("|","/") .. "|" .. row.ResourceType .. "|" .. rClass .. "|" .. ourAmt .. "|" .. theirAmt)
    end
end
local hasOB = false
pcall(function() hasOB = pDiplo:HasOpenBordersFrom(target) end)
if not hasOB then pcall(function() hasOB = pDiplo:GetVisibilityOn(target) >= 2 end) end
print("OB|" .. (hasOB and "1" or "0"))
local ai = Players[target]:GetDiplomaticAI()
local stateIdx = ai:GetDiplomaticStateIndex(me)
local hasDiploService = false
pcall(function()
    local civic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
    if civic then hasDiploService = Players[me]:GetCulture():HasCivic(civic.Index) end
end)
local allianceEligible = (stateIdx == 1 and hasDiploService)
local currentAlliance = ""
if stateIdx == 0 then
    local ok3, aType = pcall(function() return pDiplo:GetAllianceType(target) end)
    if ok3 and aType and aType >= 0 then
        local aNames = {{"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}}
        currentAlliance = aNames[aType+1] or ""
    end
end
print("ALLIANCE|" .. (allianceEligible and "1" or "0") .. "|" .. currentAlliance)
for _, city in Players[me]:GetCities():Members() do
    local cName = Locale.Lookup(city:GetName()):gsub("|", "/")
    local cid = city:GetID()
    local pop = city:GetPopulation()
    local isCapital = city:IsCapital() and "1" or "0"
    print("CITY|OURS|" .. cid .. "|" .. cName .. "|" .. pop .. "|" .. isCapital)
end
for _, city in Players[target]:GetCities():Members() do
    local cName = Locale.Lookup(city:GetName()):gsub("|", "/")
    local cid = city:GetID()
    local pop = city:GetPopulation()
    local isCapital = city:IsCapital() and "1" or "0"
    print("CITY|THEIRS|" .. cid .. "|" .. cName .. "|" .. pop .. "|" .. isCapital)
end
print("{SENTINEL}")
"""


def parse_deal_options_response(lines: list[str]) -> DealOptions:
    """Parse the deal options query response."""
    opts = DealOptions(other_player_id=0, other_civ_name="")
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) >= 3:
                opts.other_player_id = int(parts[1])
                opts.other_civ_name = parts[2]
        elif line.startswith("ECON|"):
            parts = line.split("|")
            if len(parts) >= 7:
                opts.our_gold = int(parts[1])
                opts.our_gpt = int(parts[2])
                opts.our_favor = int(parts[3])
                opts.their_gold = int(parts[4])
                opts.their_gpt = int(parts[5])
                opts.their_favor = int(parts[6])
        elif line.startswith("RES|"):
            parts = line.split("|")
            if len(parts) >= 6:
                name = parts[1]
                res_class = parts[3]
                our_amt = int(parts[4])
                their_amt = int(parts[5])
                is_luxury = "LUXURY" in res_class
                is_strategic = "STRATEGIC" in res_class
                if our_amt > 0:
                    label = f"{name} x{our_amt}" if our_amt > 1 else name
                    if is_luxury:
                        opts.our_luxuries.append(label)
                    elif is_strategic:
                        opts.our_strategics.append(label)
                if their_amt > 0:
                    label = f"{name} x{their_amt}" if their_amt > 1 else name
                    if is_luxury:
                        opts.their_luxuries.append(label)
                    elif is_strategic:
                        opts.their_strategics.append(label)
        elif line.startswith("OB|"):
            opts.has_open_borders = line.split("|")[1] == "1"
        elif line.startswith("ALLIANCE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                opts.alliance_eligible = parts[1] == "1"
                if parts[2]:
                    opts.current_alliance = parts[2]
        elif line.startswith("CITY|"):
            parts = line.split("|")
            if len(parts) >= 6:
                city = TradeableCity(
                    city_id=int(parts[2]),
                    name=parts[3],
                    population=int(parts[4]),
                    is_capital=parts[5] == "1",
                )
                if parts[1] == "OURS":
                    opts.our_cities.append(city)
                else:
                    opts.their_cities.append(city)
    return opts


def build_pending_deals_query() -> str:
    """Scan all met players for incoming trade deal offers (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        -- 2026-08-03 实机修复:此前这里要求 FindOpenSessionID(me,i) >= 0 才往下读,
        -- 即【必须已经打开那个外交会话】。但 AI 主动发来的提议(尤其是求和)
        -- 是以通知/弹窗的形式挂着的 —— 人没点开它就没有 open session,
        -- 于是本查询一路跳过,对外报「没有待处理交易」,而游戏里明明有一个
        -- 弹窗在等响应(还会把回合卡住,实测 T116 出现过 116->116 不推进)。
        -- 现在无条件尝试读 INCOMING working deal;有 session 时额外标注。
        local sid = -1
        pcall(function() sid = DiplomacyManager.FindOpenSessionID(me, i) or -1 end)
        local ok, deal = pcall(function() return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, i) end)
        if ok and deal then
            local count = deal:GetItemCount()
            if count and count > 0 then
                local cfg = PlayerConfigurations[i]
                local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
                local leaderName = Locale.Lookup(cfg:GetLeaderName())
                -- sid < 0 表示这个提议还挂在通知/弹窗里、尚未打开会话;
                -- respond_to_deal 会自己补开会话,调用方不必先点开。
                print("DEAL|" .. i .. "|" .. civName:gsub("|","/") .. "|" .. leaderName:gsub("|","/") .. "|sid=" .. tostring(sid))
                for item in deal:Items() do
                    local fromID = item:GetFromPlayerID()
                    local iType = item:GetType()
                    local subType = item:GetSubType()
                    local amount = item:GetAmount() or 0
                    local duration = item:GetDuration() or 0
                    local valueType = item:GetValueType() or -1
                    local typeName = "UNKNOWN"
                    local itemName = "Unknown"
                    if iType == DealItemTypes.GOLD then
                        typeName = "GOLD"
                        if duration > 0 then itemName = "Gold per turn" else itemName = "Gold (lump sum)" end
                    elseif iType == DealItemTypes.RESOURCES then
                        typeName = "RESOURCE"
                        local res = GameInfo.Resources[valueType]
                        if res then itemName = Locale.Lookup(res.Name) else itemName = "Resource#" .. tostring(valueType) end
                    elseif iType == DealItemTypes.AGREEMENTS then
                        typeName = "AGREEMENT"
                        if subType == DealAgreementTypes.OPEN_BORDERS then itemName = "Open Borders"
                        elseif subType == DealAgreementTypes.JOINT_WAR then
                            -- 联合宣战的 valueType = 目标玩家 ID(2026-08-05:
                            -- 此前不发,接受邀请的宣战门禁无从查目标)。
                            -- 目标 ID 借道 amount 字段(协议项本无金额)。
                            itemName = "Joint War"
                            pcall(function()
                                local tcfg = PlayerConfigurations[valueType]
                                if tcfg then itemName = "Joint War vs " .. Locale.Lookup(tcfg:GetCivilizationShortDescription()) end
                            end)
                            amount = valueType
                        elseif subType == DealAgreementTypes.ALLIANCE then
                            local aNames = {"Research","Cultural","Economic","Military","Religious"}
                            itemName = (valueType >= 0 and valueType < 5 and aNames[valueType+1] or "Unknown") .. " Alliance"
                        elseif subType == DealAgreementTypes.MAKE_PEACE then
                            itemName = "Peace Treaty"
                        else
                            -- 2026-08-04 P0:未知协议类型必须显形,不许设空被过滤。
                            -- 实机:阿拉伯求和(MAKE_PEACE)曾因映射缺失被静默丢弃,
                            -- 简报里完全不显示。未知子类型显示 Agreement#<subType>。
                            itemName = "Agreement#" .. tostring(subType)
                        end
                    elseif iType == DealItemTypes.FAVOR then
                        typeName = "FAVOR"
                        itemName = "Diplomatic Favor"
                    elseif iType == DealItemTypes.CITIES then
                        typeName = "CITY"
                        itemName = "City"
                    elseif iType == DealItemTypes.GREATWORK then
                        typeName = "GREAT_WORK"
                        itemName = "Great Work"
                    end
                    if itemName ~= "" and itemName ~= "Unknown" then
                        local fromTag = "THEM"
                        if fromID == me then fromTag = "US" end
                        print("ITEM|" .. i .. "|" .. fromTag .. "|" .. typeName .. "|" .. itemName:gsub("|","/") .. "|" .. amount .. "|" .. duration)
                    end
                end
            end
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_respond_to_deal(other_player_id: int, accept: bool) -> str:
    """Accept or reject a pending trade deal (InGame context)."""
    action = "DealProposalAction.ACCEPTED" if accept else "DealProposalAction.REJECTED"
    verb = "ACCEPTED" if accept else "REJECTED"
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local sid = -1
pcall(function() sid = DiplomacyManager.FindOpenSessionID(me, target) or -1 end)
-- 2026-08-03:AI 主动发来的提议挂在通知/弹窗里,尚未打开会话。
-- 此前这里直接报 NO_DEAL,于是「看得到、响应不了」。现在自己补开会话。
if sid < 0 then
    pcall(function() DiplomacyManager.RequestSession(me, target, "MAKE_DEAL") end)
    pcall(function() sid = DiplomacyManager.FindOpenSessionID(me, target) or -1 end)
end
if sid < 0 then {_bail(f"ERR:NO_DEAL|No deal session with player {other_player_id} (tried to open one)")} end
DealManager.SendWorkingDeal({action}, me, target)
-- 2026-08-04 修复:响应后清掉 INCOMING working deal。否则同一份提议
-- 会残留,下一次 get_pending_trades 继续报它(本会话 8+ 次复现,
-- 每次都要手动 ClearWorkingDeal 绕)。ACCEPTED/REJECTED 都要清,
-- ClearWorkingDeal 幂等,重复清理无害。
pcall(function() DealManager.ClearWorkingDeal(DealDirection.INCOMING, me, target) end)
{_lua_close_diplo_session()}
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("OK:DEAL_{verb}|" .. name)
print("{SENTINEL}")
"""


def _lua_deal_item(from_var: str, item: dict) -> str:
    """Generate Lua snippet to add one item to the working deal.

    from_var: Lua variable name for the player ID (e.g. "me" or "target").
    item: dict with keys type, amount, and optionally name, duration.
    """
    t = item["type"].upper()
    amount = item.get("amount", 0)
    duration = item.get("duration", 0)

    if t == "GOLD":
        return (
            f"do local gi = deal:AddItemOfType(DealItemTypes.GOLD, {from_var}) "
            f"if gi then gi:SetAmount({amount}) gi:SetDuration({duration}) end end"
        )
    elif t == "RESOURCE":
        res_name = item["name"]
        res_amount = item.get("amount", 1)
        res_duration = item.get("duration", 30)
        # 2026-08-09:加入失败必须写 dealErr 而不是静默跳过 —— 静默跳过 +
        # working deal 残留 = 把上一笔的资源当成这一笔发出去(T287 鲸鱼变皮草)。
        return (
            f'do local res = GameInfo.Resources["{res_name}"] '
            f'if not res then dealErr = dealErr or "unknown resource {res_name}" '
            f"else local ri = deal:AddItemOfType(DealItemTypes.RESOURCES, {from_var}) "
            f'if not ri then dealErr = dealErr or "engine refused resource item {res_name}" '
            f"else ri:SetValueType(res.Index) ri:SetAmount({res_amount}) "
            f"ri:SetDuration({res_duration}) end end end"
        )
    elif t == "FAVOR":
        return (
            f"do local fi = deal:AddItemOfType(DealItemTypes.FAVOR, {from_var}) "
            f"if fi then fi:SetAmount({amount}) end end"
        )
    elif t == "AGREEMENT":
        subtype = item["subtype"]  # "OPEN_BORDERS", "JOINT_WAR", "ALLIANCE"
        # JOINT_WAR 等协议需要 SetValueType(第三方玩家 ID) 指明对象 ——
        # 2026-08-05 复查发现 joint_war_target 此前根本没传到引擎,
        # 联合宣战交易一直是残缺 deal。
        target_player = int(item.get("target_player") or -1)
        set_target = f"ai:SetValueType({target_player}) " if target_player >= 0 else ""
        # 2026-08-10 罗马局 T130-T135 实机(缺陷64):协议项不设 SetDuration 时,
        # 0 时长的开放边界能通过 AI 评估、能在成交条款里正常显示,但【结算为
        # 空操作】—— 三笔 ACCEPTED 的开放边界交易(含 100 金代价)全部没生效,
        # HasOpenBordersFrom 恒 false。补 SetDuration(30) 后同款交易当帧生效,
        # 且 pending 展示从「Open Borders」变为「Open Borders for 30 turns」。
        agreement_duration = int(item.get("duration") or 30)
        return (
            f"do local ai = deal:AddItemOfType(DealItemTypes.AGREEMENTS, {from_var}) "
            f"if ai then ai:SetSubType(DealAgreementTypes.{subtype}) "
            f"ai:SetDuration({agreement_duration}) {set_target}end end"
        )
    elif t == "CITY":
        city_id = item["city_id"]
        return (
            f"do local ci = deal:AddItemOfType(DealItemTypes.CITIES, {from_var}) "
            f"if ci then ci:SetValueType({city_id}) end end"
        )
    else:
        return f"-- unsupported deal item type: {t}"


def _deal_has_peace(offer_items: list[dict], request_items: list[dict]) -> bool:
    """这笔提案里是否带停战条款。

    2026-08-12 缺陷114:交战中原本一律拒绝提案,而「用和解换城市和资源」
    恰恰只能在交战时谈 —— 停战本身就是我方付出的对价。带了 MAKE_PEACE
    就该放行,没带才拒(没带的话引擎本来也会拒)。
    """
    for it in list(offer_items or ()) + list(request_items or ()):
        if str(it.get("type", "")).upper() != "AGREEMENT":
            continue
        if str(it.get("subtype", "")).upper() == "MAKE_PEACE":
            return True
    return False


def _war_gate(offer_items: list[dict], request_items: list[dict]) -> str:
    """交战时的拦截代码;带了停战条款就返回空串(放行)。"""
    if _deal_has_peace(offer_items, request_items):
        return ""
    return _bail(
        "ERR:AT_WAR|交战中提案必须带停战条款 —— 传 make_peace=True。"
        "停战是你在和约里付出的对价,劣势方会拿城市和资源来换它;"
        "不带这一条,引擎本来也不会受理。"
    )


def build_propose_trade(
    other_player_id: int,
    offer_items: list[dict],
    request_items: list[dict],
) -> str:
    """Build a trade deal proposal and send it (InGame context).

    offer_items: items we give to them (from us).
    request_items: items we want from them.
    Each item dict: {type: GOLD|RESOURCE|FAVOR|AGREEMENT|CITY, amount: int, name: str, duration: int, subtype: str, city_id: int}
    """
    offer_lua = " ".join(_lua_deal_item("me", item) for item in offer_items)
    request_lua = " ".join(_lua_deal_item("target", item) for item in request_items)

    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met player " + str(other_player_id))} end
if pDiplo:IsAtWarWith(target) then {_war_gate(offer_items, request_items)} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
-- 2026-08-09 T287 实机:上一笔成交后的 working deal 残留(HasPendingDeal 短暂
-- 为 true 时跳过清空),残留里还躺着上次的皮草 item;新 offer 的鲸鱼 item
-- AddItemOfType 失败被 if ri then 静默吞掉 → 发出去的 deal 是旧皮草。
-- 表现:offer_resources=RESOURCE_WHALES,成交条款 "We give: 1x 皮草"。
-- 出向工作区必须无条件清空 —— 它只影响我方待发提案,不碰 incoming。
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
local dealErr = nil
{offer_lua}
{request_lua}
if dealErr then
    DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
    {_bail_lua('"ERR:DEAL_ITEM|" .. dealErr')}
end
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
local sid = DiplomacyManager.FindOpenSessionID(me, target)
local result = "PROPOSED"
local termsStr = ""
if sid and sid >= 0 then
    local ok, respDeal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
    end)
    if ok and respDeal and respDeal:GetItemCount() and respDeal:GetItemCount() > 0 then
        DealManager.SendWorkingDeal(DealProposalAction.ACCEPTED, me, target)
        result = "ACCEPTED"
        -- Report the actual deal terms (AI may have counter-offered different terms)
        local weGive = {{}}
        local theyGive = {{}}
        for item in respDeal:Items() do
            local fromID = item:GetFromPlayerID()
            local iType = item:GetType()
            local subType = item:GetSubType() or -1
            local amount = item:GetAmount() or 0
            local duration = item:GetDuration() or 0
            local desc = "Unknown"
            if iType == DealItemTypes.GOLD then
                if duration > 0 then desc = amount .. " gold/turn (" .. duration .. " turns)"
                else desc = amount .. " gold" end
            elseif iType == DealItemTypes.AGREEMENTS then
                if subType == DealAgreementTypes.OPEN_BORDERS then desc = "Open Borders"
                elseif subType == DealAgreementTypes.JOINT_WAR then desc = "Joint War"
                else desc = "Agreement" end
            elseif iType == DealItemTypes.RESOURCES then
                local res = GameInfo.Resources[item:GetValueType() or -1]
                local rName = res and Locale.Lookup(res.Name) or "Resource"
                if duration > 0 then desc = amount .. "x " .. rName .. " (" .. duration .. "t)"
                else desc = amount .. "x " .. rName end
            elseif iType == DealItemTypes.FAVOR then
                desc = amount .. " Diplomatic Favor"
            elseif iType == DealItemTypes.CITIES then
                desc = "City"
            end
            if fromID == me then table.insert(weGive, desc)
            else table.insert(theyGive, desc) end
        end
        if #weGive > 0 or #theyGive > 0 then
            termsStr = "\\nActual deal terms:"
            if #weGive > 0 then termsStr = termsStr .. "\\n  We give: " .. table.concat(weGive, ", ") end
            if #theyGive > 0 then termsStr = termsStr .. "\\n  They give: " .. table.concat(theyGive, ", ") end
        end
    else
        result = "REJECTED"
    end
    -- 2026-08-10 罗马局 T210(缺陷79a):成交后工作交易缓冲区不清,同一笔交易
    -- 会以【incoming 挂起交易】的形式再出现一次,必须 respond_to_trade 再接
    -- 一遍 —— 三笔奢侈品外销全都要走两道手续,而 end_turn 会被"有挂起交易"
    -- 直接拦住。成交已由 ACCEPTED 落地,缓冲区留着只会制造这个幽灵。
    DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
    pcall(function() DealManager.ClearWorkingDeal(DealDirection.INCOMING, me, target) end)
    {_lua_close_diplo_session()}
end
print("OK:" .. result .. "|Trade " .. result:lower() .. " with " .. name .. termsStr)
print("{SENTINEL}")
"""


def build_test_trade(
    other_player_id: int,
    offer_items: list[dict],
    request_items: list[dict],
) -> str:
    """Test a trade deal via EQUALIZE — returns what the AI thinks is fair (InGame).

    Same item format as build_propose_trade. Does NOT commit the deal.
    """
    offer_lua = " ".join(_lua_deal_item("me", item) for item in offer_items)
    request_lua = " ".join(_lua_deal_item("target", item) for item in request_items)

    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met player " + str(other_player_id))} end
if pDiplo:IsAtWarWith(target) then {_war_gate(offer_items, request_items)} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("CIV|" .. target .. "|" .. name:gsub("|","/"))
pcall(function()
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if sid and sid >= 0 then DiplomacyManager.CloseSession(sid) end
end)
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
{offer_lua}
{request_lua}
print("PROPOSED_ITEMS")
for item in deal:Items() do
    local fromTag = item:GetFromPlayerID() == me and "US" or "THEM"
    local itype = item:GetType()
    local typeName = "UNKNOWN"
    local ok3, vid = pcall(function() return item:GetValueTypeID() end); vid = (ok3 and vid) and tostring(vid) or ""
    local ok4, sid2 = pcall(function() return item:GetSubTypeID() end); sid2 = (ok4 and sid2) and tostring(sid2) or ""
    local amount = item:GetAmount() or 0
    local duration = item:GetDuration() or 0
    if itype == DealItemTypes.GOLD then typeName = "GOLD"
    elseif itype == DealItemTypes.RESOURCES then typeName = "RESOURCE"
    elseif itype == DealItemTypes.AGREEMENTS then typeName = "AGREEMENT"
    elseif itype == DealItemTypes.FAVOR then typeName = "FAVOR"
    elseif itype == DealItemTypes.CITIES then typeName = "CITY"
    elseif itype == DealItemTypes.GREATWORK then typeName = "GREAT_WORK"
    end
    print("ITEM|" .. fromTag .. "|" .. typeName .. "|" .. amount .. "|" .. duration .. "|" .. vid .. "|" .. sid2)
end
DealManager.SendWorkingDeal(DealProposalAction.EQUALIZE, me, target)
local inDeal = DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
if inDeal and inDeal:GetItemCount() and inDeal:GetItemCount() > 0 then
    print("AI_COUNTER")
    for item in inDeal:Items() do
        local fromTag = item:GetFromPlayerID() == me and "US" or "THEM"
        local itype = item:GetType()
        local typeName = "UNKNOWN"
        local ok3, vid = pcall(function() return item:GetValueTypeID() end); vid = (ok3 and vid) and tostring(vid) or ""
        local ok4, sid2 = pcall(function() return item:GetSubTypeID() end); sid2 = (ok4 and sid2) and tostring(sid2) or ""
        local amount = item:GetAmount() or 0
        local duration = item:GetDuration() or 0
        if itype == DealItemTypes.GOLD then typeName = "GOLD"
        elseif itype == DealItemTypes.RESOURCES then typeName = "RESOURCE"
        elseif itype == DealItemTypes.AGREEMENTS then typeName = "AGREEMENT"
        elseif itype == DealItemTypes.FAVOR then typeName = "FAVOR"
        elseif itype == DealItemTypes.CITIES then typeName = "CITY"
        elseif itype == DealItemTypes.GREATWORK then typeName = "GREAT_WORK"
        end
        print("ITEM|" .. fromTag .. "|" .. typeName .. "|" .. amount .. "|" .. duration .. "|" .. vid .. "|" .. sid2)
    end
else
    print("AI_COUNTER")
    print("REJECTED")
end
-- 2026-08-10 罗马局 T210 实机(缺陷79):EQUALIZE 会把工作交易【真的提交】给
-- 对方,读完还价只关会话是不够的 —— 缓冲区里的提案会留下来变成一笔真实的
-- 挂起交易。实测:给西班牙 mode="test" 试价汞,当回合 end_turn 就被
-- "incoming trade deal pending" 拦下,里面躺着西班牙 1 金/回合的还价;
-- 拒掉它还吃了「您违背了诺言」的外交惩罚。
-- 试价本该是零副作用的比价手段(同一份汞在三家之间差 6 倍,不试价就亏),
-- 一个会留雷、还会伤关系的"预览"比不提供预览更糟。双向缓冲区都要清空。
DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
pcall(function() DealManager.ClearWorkingDeal(DealDirection.INCOMING, me, target) end)
pcall(function()
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if sid and sid >= 0 then DiplomacyManager.CloseSession(sid) end
end)
print("{SENTINEL}")
"""


def parse_test_trade_response(lines: list[str]) -> TestTradeResult:
    """Parse the test trade response.

    Raises RuntimeError on an ERR: line. 2026-08-10 罗马局 T199(缺陷78):
    我把俄罗斯的 player id 猜成了 9(实际 5),lua 正确地报了 ERR:NOT_MET,
    但这里只找 CIV|/ITEM| 行、找不到就返回默认值 —— 于是渲染出
    "Trade test with  (player 0) / 你的提案:(空) / AI: ACCEPTABLE",
    一个不存在的对手对一份空提案表示接受。错误必须冒泡,不能退化成
    看起来正常的空结果。
    """
    for line in lines:
        if line.startswith("ERR:"):
            raise RuntimeError(line[4:])
    result = TestTradeResult(
        other_player_id=0,
        other_civ_name="",
        proposed=[],
        counter=[],
        rejected=False,
    )
    section = ""
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) >= 3:
                result.other_player_id = int(parts[1])
                result.other_civ_name = parts[2]
        elif line == "PROPOSED_ITEMS":
            section = "proposed"
        elif line == "AI_COUNTER":
            section = "counter"
        elif line == "REJECTED":
            result.rejected = True
        elif line.startswith("ITEM|") and section:
            parts = line.split("|")
            if len(parts) >= 7:
                item = TestTradeItem(
                    side=parts[1],
                    item_type=parts[2],
                    amount=int(parts[3]),
                    duration=int(parts[4]),
                    value_id=parts[5],
                    subtype_id=parts[6],
                )
                if section == "proposed":
                    result.proposed.append(item)
                else:
                    result.counter.append(item)
    return result


def build_form_alliance(other_player_id: int, alliance_type: str) -> str:
    """Form an alliance with another civilization (InGame context).

    alliance_type: MILITARY, RESEARCH, CULTURAL, ECONOMIC, RELIGIOUS
    """
    alliance_key = f"ALLIANCE_{alliance_type.upper()}"
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local allianceRow = GameInfo.Alliances["{alliance_key}"]
local type_idx = allianceRow and allianceRow.Index or 0
local pDiplo = Players[me]:GetDiplomacy()
if not Players[target] or not Players[target]:IsAlive() then {_bail("ERR:INVALID_PLAYER|Player not found")} end
if not pDiplo:HasMet(target) then {_bail("ERR:NOT_MET|Have not met this civilization")} end
if pDiplo:IsAtWarWith(target) then {_bail("ERR:AT_WAR|Cannot ally while at war")} end
local ai = Players[target]:GetDiplomaticAI()
local stateIdx = ai:GetDiplomaticStateIndex(me)
if stateIdx == 0 then {_bail("ERR:ALREADY_ALLIED|Already in an alliance")} end
if stateIdx ~= 1 then {_bail("ERR:NOT_FRIENDS|Must be declared friends first")} end
local hasDiploService = false
pcall(function()
    local civic = GameInfo.Civics["CIVIC_DIPLOMATIC_SERVICE"]
    if civic then hasDiploService = Players[me]:GetCulture():HasCivic(civic.Index) end
end)
if not hasDiploService then {_bail("ERR:NO_CIVIC|Diplomatic Service civic required for alliances")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
if not DealManager.HasPendingDeal(me, target) then
    DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
end
local deal = DealManager.GetWorkingDeal(DealDirection.OUTGOING, me, target)
if not deal then {_bail("ERR:NO_DEAL_OBJECT|Failed to get working deal")} end
do local ai_item = deal:AddItemOfType(DealItemTypes.AGREEMENTS, me)
-- 缺陷64同源:协议项必须带时长,否则结算为空操作(联盟同为 30 回合协议)
if ai_item then ai_item:SetSubType(DealAgreementTypes.ALLIANCE) ai_item:SetDuration(30) pcall(function() ai_item:SetValueType(type_idx) end) end end
DiplomacyManager.RequestSession(me, target, "MAKE_DEAL")
DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
local sid = DiplomacyManager.FindOpenSessionID(me, target)
local result = "PROPOSED"
if sid and sid >= 0 then
    local ok, respDeal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, target)
    end)
    if ok and respDeal then
        local itemCount = 0
        pcall(function() itemCount = respDeal:GetItemCount() or 0 end)
        if itemCount > 0 then
            DealManager.SendWorkingDeal(DealProposalAction.ACCEPTED, me, target)
            result = "ACCEPTED"
        else
            result = "REJECTED"
        end
    else
        result = "REJECTED"
    end
    {_lua_close_diplo_session()}
end
local postState = Players[target]:GetDiplomaticAI():GetDiplomaticStateIndex(me)
if postState == 0 then
    local aNames = {{"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}}
    local typeName = "{alliance_type}"
    local ok3, aType = pcall(function() return pDiplo:GetAllianceType(target) end)
    if ok3 and aType and aType >= 0 then typeName = aNames[aType+1] or typeName end
    print("OK:ACCEPTED|" .. typeName .. " alliance formed with " .. name)
else
    if result == "REJECTED" then
        print("OK:REJECTED|" .. name .. " rejected the " .. "{alliance_type}" .. " alliance proposal")
    else
        print("OK:FAILED|Alliance proposal sent but status unclear (state=" .. tostring(postState) .. ")")
    end
end
print("{SENTINEL}")
"""


def build_propose_peace(other_player_id: int) -> str:
    """Propose white peace to a civilization we're at war with (InGame context).

    Session type is "MAKE_PEACE" (not "PROPOSE_PEACE_DEAL" which silently fails).
    After sending the deal, close the session with NEGATIVE+CloseSession loop,
    then ShowIngameUI + Events.HideLeaderScreen() to restore HUD and dismiss 3D leader.
    """
    return f"""
local me = Game.GetLocalPlayer()
local target = {other_player_id}
local pDiplo = Players[me]:GetDiplomacy()
if not pDiplo:IsAtWarWith(target) then {_bail("ERR:NOT_AT_WAR|Not at war with player " + str(other_player_id))} end
local canPeace = pDiplo:CanMakePeaceWith(target)
if not canPeace then {_bail("ERR:CANNOT_MAKE_PEACE|10-turn war cooldown or other restriction")} end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
    DiplomacyManager.RequestSession(me, target, "MAKE_PEACE")
    local sid = DiplomacyManager.FindOpenSessionID(me, target)
    if not sid or sid < 0 then {_bail("ERR:NO_SESSION|Failed to open peace deal session")} end
    DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)
    DealManager.SendWorkingDeal(DealProposalAction.PROPOSED, me, target)
    print("OK:PROPOSED|" .. name)
    print("{SENTINEL}")
"""


def build_check_war_state(other_player_id: int) -> str:
    """Check if we're still at war with a player (InGame context)."""
    return f"""
local me = Game.GetLocalPlayer()
local atWar = Players[me]:GetDiplomacy():IsAtWarWith({other_player_id})
print(atWar and "AT_WAR" or "AT_PEACE")
print("{SENTINEL}")
"""


def parse_diplomacy_response(lines: list[str]) -> list[CivInfo]:
    civs: dict[int, CivInfo] = {}
    for line in lines:
        if line.startswith("CIV|"):
            parts = line.split("|")
            if len(parts) < 13:
                continue
            pid = int(parts[1])
            civs[pid] = CivInfo(
                player_id=pid,
                civ_name=parts[2],
                leader_name=parts[3],
                has_met=parts[4] == "1",
                is_at_war=parts[5] == "1",
                diplomatic_state=parts[6],
                grievances=int(parts[7]),
                access_level=int(parts[8]),
                has_delegation=parts[9] == "1",
                has_embassy=parts[10] == "1",
                they_have_delegation=parts[11] == "1",
                they_have_embassy=parts[12] == "1",
                modifiers=[],
                available_actions=[],
            )
        elif line.startswith("LEADERTYPE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs and parts[2] and parts[2] != "nil":
                    civs[pid].leader_type = parts[2]
        elif line.startswith("MOD|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].modifiers.append(
                        DiplomacyModifier(
                            score=int(parts[2]),
                            text=parts[3],
                        )
                    )
                    civs[pid].relationship_score += int(parts[2])
        elif line.startswith("ALLIANCE|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].alliance_type = parts[2]
                    if len(parts) >= 4:
                        try:
                            civs[pid].alliance_level = int(parts[3])
                        except ValueError:
                            pass
        elif line.startswith("MILITARY|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                if pid in civs:
                    try:
                        civs[pid].military_strength = int(parts[2])
                        civs[pid]._our_military = int(parts[3])  # type: ignore[attr-defined]
                    except ValueError:
                        pass
        elif line.startswith("ECITY|"):
            parts = line.split("|")
            if len(parts) >= 9:
                pid = int(parts[1])
                if pid in civs:
                    xy = parts[3].split(",")
                    try:
                        vc = VisibleCity(
                            name=parts[2],
                            x=int(xy[0]),
                            y=int(xy[1]),
                            population=int(parts[4]),
                            loyalty=float(parts[5]),
                            loyalty_per_turn=float(parts[6]),
                            has_walls=int(parts[7]) > 0,
                            defense_strength=int(parts[8]),
                            # 旧格式(9 字段)没有首都位 → 默认 False,不谎报
                            is_original_capital=len(parts) > 9 and parts[9] == "1",
                        )
                        civs[pid].visible_cities.append(vc)
                    except (ValueError, IndexError):
                        pass
        elif line.startswith("CIVCITIES|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].num_cities = int(parts[2])
        elif line.startswith("ACTIONS|"):
            parts = line.split("|")
            if len(parts) >= 3:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].available_actions = parts[2].split(",")
        elif line.startswith("AGENDA|"):
            parts = line.split("|")
            if len(parts) >= 5:
                pid = int(parts[1])
                if pid in civs:
                    civs[pid].agendas.append(
                        AgendaInfo(
                            category=parts[2],
                            name=parts[3],
                            description=parts[4],
                        )
                    )
        elif line.startswith("PACT|"):
            parts = line.split("|")
            if len(parts) == 3:
                # PACT|pid|DEFENSIVE — pact between us and pid
                pid = int(parts[1])
                if pid in civs:
                    # Mark that this civ has a defensive pact (with us)
                    pass  # We don't track pacts with us specially
            elif len(parts) == 4:
                # PACT|pid1|pid2|DEFENSIVE — third-party pact
                pid1, pid2 = int(parts[1]), int(parts[2])
                if pid1 in civs:
                    civs[pid1].defensive_pacts.append(pid2)
                if pid2 in civs:
                    civs[pid2].defensive_pacts.append(pid1)
    return list(civs.values())


def parse_diplomacy_sessions(lines: list[str]) -> list[DiplomacySession]:
    """Parse open diplomacy session output."""
    sessions = []
    for line in lines:
        if line == "NONE":
            break
        if line.startswith("SESSION|"):
            parts = line.split("|")
            if len(parts) >= 5:
                sessions.append(
                    DiplomacySession(
                        session_id=int(parts[1]),
                        other_player_id=int(parts[2]),
                        other_civ_name=parts[3],
                        other_leader_name=parts[4],
                        choices=[],
                        dialogue_text=parts[5] if len(parts) > 5 else "",
                        reason_text=parts[6] if len(parts) > 6 else "",
                        buttons=parts[7] if len(parts) > 7 else "",
                        is_at_war=parts[8] == "1" if len(parts) > 8 else False,
                    )
                )
        elif line.startswith("DEAL_ITEM|") and sessions:
            # DEAL_ITEM|playerID|fromTag|typeName|itemName|amount|duration
            parts = line.split("|")
            if len(parts) >= 5:
                from_tag = parts[2]  # "THEM" or "US"
                item_name = parts[4]
                amount = int(parts[5]) if len(parts) > 5 else 0
                duration = int(parts[6]) if len(parts) > 6 else 0
                dur_str = f" ({duration} turns)" if duration > 0 else ""
                amt_str = f" x{amount}" if amount > 0 else ""
                entry = f"{'They offer' if from_tag == 'THEM' else 'You offer'}: {item_name}{amt_str}{dur_str}"
                s = sessions[-1]
                s.deal_summary = (
                    (s.deal_summary + "; " + entry) if s.deal_summary else entry
                )
    return sessions


def parse_pending_deals_response(lines: list[str]) -> list[PendingDeal]:
    """Parse DEAL| and ITEM| lines from build_pending_deals_query."""
    deals: dict[int, PendingDeal] = {}
    for line in lines:
        if line.startswith("DEAL|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pid = int(parts[1])
                deals[pid] = PendingDeal(
                    other_player_id=pid,
                    other_player_name=parts[2],
                    other_leader_name=parts[3],
                )
        elif line.startswith("ITEM|"):
            parts = line.split("|")
            if len(parts) >= 7:
                pid = int(parts[1])
                if pid not in deals:
                    continue
                is_from_us = parts[2] == "US"
                item = DealItem(
                    from_player_id=-1 if is_from_us else pid,
                    from_player_name="Us"
                    if is_from_us
                    else deals[pid].other_player_name,
                    item_type=parts[3],
                    name=parts[4],
                    amount=int(parts[5]),
                    duration=int(parts[6]),
                    is_from_us=is_from_us,
                )
                if is_from_us:
                    deals[pid].items_from_us.append(item)
                else:
                    deals[pid].items_from_them.append(item)
    return list(deals.values())
