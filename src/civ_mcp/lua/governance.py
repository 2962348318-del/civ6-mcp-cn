"""Governance domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import (
    _LUA_RESOLVE_UU,
    _LUA_XP_THRESHOLD,
    SENTINEL,
    _bail,
    _bail_lua,
    _int,
    _lua_get_city,
    _lua_get_unit,
    _lua_get_unit_gamecore,
)
from civ_mcp.lua.models import (
    AppointedGovernor,
    CityStateInfo,
    DedicationChoice,
    DedicationStatus,
    EnvoyStatus,
    GovernmentStatus,
    GovernorInfo,
    GovernorPromotion,
    GovernorStatus,
    PolicyInfo,
    PolicySlot,
    PromotionOption,
    UnitPromotionStatus,
)


def build_policies_query() -> str:
    """Read current government, policy slots, and available policies (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()
local govIdx = pCulture:GetCurrentGovernment()
local govName = "None"
local govType = "NONE"
if govIdx and govIdx >= 0 then
    local govEntry = GameInfo.Governments[govIdx]
    if govEntry then
        govName = Locale.Lookup(govEntry.Name)
        govType = govEntry.GovernmentType
    end
end
local numSlots = pCulture:GetNumPolicySlots()
print("GOV|" .. govType .. "|" .. govName:gsub("|","/") .. "|" .. numSlots)
local slotNames = {[0]="SLOT_ECONOMIC", [1]="SLOT_MILITARY", [2]="SLOT_DIPLOMATIC", [3]="SLOT_WILDCARD", [4]="SLOT_WILDCARD"}
for s = 0, numSlots - 1 do
    local slotType = slotNames[pCulture:GetSlotType(s)] or ("SLOT_" .. pCulture:GetSlotType(s))
    local policyIdx = pCulture:GetSlotPolicy(s)
    local policyType = "NONE"
    local policyName = "Empty"
    if policyIdx >= 0 then
        local pe = GameInfo.Policies[policyIdx]
        if pe then
            policyType = pe.PolicyType
            policyName = Locale.Lookup(pe.Name)
        end
    end
    print("SLOT|" .. s .. "|" .. slotType .. "|" .. policyType .. "|" .. policyName:gsub("|","/"))
end
-- 缺陷104(2026-08-11 罗马局 T304-T305):这里只查 IsPolicyUnlocked。
-- 过时卡(被后继卡取代的,如征兵→全民动员)【仍然是 unlocked】,而且
-- CanSlotPolicy 对它照样返回 true —— 它只查槽位类型,不查过时。于是
-- 征兵/侍从/城市规划三张僵尸卡混进了可用清单,我照单挑走,两次提交、
-- 三个回合,一张都没进槽。引擎侧的判据只有 IsPolicyObsolete 一个。
for policy in GameInfo.Policies() do
    if pCulture:IsPolicyUnlocked(policy.Index)
            and not pCulture:IsPolicyObsolete(policy.Index) then
        local canSlot = false
        for s = 0, numSlots - 1 do
            if pCulture:CanSlotPolicy(policy.Index, s) then
                canSlot = true
                break
            end
        end
        if canSlot then
            local slotType = "SLOT_WILDCARD"
            if policy.GovernmentSlotType then slotType = policy.GovernmentSlotType end
            local name = Locale.Lookup(policy.Name)
            local desc = Locale.Lookup(policy.Description):gsub("|", "/"):gsub("\\n", " ")
            print("AVAIL|" .. policy.PolicyType .. "|" .. name:gsub("|","/") .. "|" .. desc .. "|" .. slotType)
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_set_policies(assignments: dict[int, str]) -> str:
    """Set policy cards in government slots (InGame context).

    assignments maps slot_index -> policy_type string.
    Slots not listed keep their current policy. Use "NONE" to explicitly clear a slot.
    Three-step: pre-checks (before UNLOCK), UNLOCK_POLICIES, then RequestPolicyChanges.

    Pre-checks run before UNLOCK_POLICIES because CanSlotPolicy is reliable there.
    It becomes stale same-frame after UNLOCK_POLICIES in the same Lua string.
    """
    pre_checks = []  # policy lookup + CanSlotPolicy + type check — BEFORE UNLOCK_POLICIES
    add_entries = []  # addList[slot] = hash — AFTER UNLOCK_POLICIES
    clear_entries = []  # clearList entries — only slots we're touching
    readback_entries = []  # 2026-08-04 P1:设置后重读每个被触碰的槽
    touched = 0

    for slot_idx, policy_type in assignments.items():
        # Always clear the slot we're about to reassign (or explicitly clearing)
        # 2026-08-04 回归回退:上一版把 clearList 改为【policy hash 数组】后,
        # 实机所有换卡都被引擎拒绝(Slot0/1/2/5 设征兵/城市规划全部失败,
        # readback 报 got 旧卡)。而旧版 clearList 传 slot 索引在实机
        # 多次换卡成功(T123/T158 等)——要么引擎接受 slot index,要么
        # 忽略 clearList 而 addList 的 slot→hash 映射生效。回退旧语义,
        # 换卡恢复可用;「服役卡被清」另有根因,待实机单独验证。
        clear_entries.append(f"table.insert(clearList, {slot_idx})")
        touched += 1

        if policy_type.upper() == "NONE":
            # Explicit clear — add to clearList but skip addList/pre-checks
            readback_entries.append(
                f"do local cur_{slot_idx} = pCulture:GetSlotPolicy({slot_idx}); "
                f"if cur_{slot_idx} and cur_{slot_idx} >= 0 then "
                f"readback_ok = false; table.insert(missing, 'NONE@{slot_idx}') end end"
            )
            continue

        pre_checks.append(
            f'local pe_{slot_idx} = GameInfo.Policies["{policy_type}"]; '
            f"if pe_{slot_idx} == nil then {_bail(f'ERR:POLICY_NOT_FOUND|{policy_type}')} end; "
            # 缺陷104:过时卡必须在这里拦死。它 IsPolicyUnlocked=true、
            # CanSlotPolicy=true、GetSlotType 也对得上,一路绿灯走到
            # RequestPolicyChanges,然后引擎【静默丢弃】—— 不抛错、不改槽、
            # PolicyChangeMade 都不置位。唯一能提前判死的是 IsPolicyObsolete。
            # 不拦的代价不是"少插一张卡",是下游那句"免费换卡窗口没开,等市政
            # 完成再改"的错误诊断:照它做,市政完成那回合会再失败一次。
            f"local obs_{slot_idx} = false; "
            f"pcall(function() obs_{slot_idx} = pCulture:IsPolicyObsolete(pe_{slot_idx}.Index) end); "
            f"if obs_{slot_idx} then "
            f"{_bail_lua(f''' "ERR:POLICY_OBSOLETE|{policy_type} 已被后继卡取代,引擎永远不会装备它(装了也是静默丢弃)。用 get_policies 重新取可用清单 —— 过时卡已从清单里滤掉。" ''')} end; "
            # CanSlotPolicy pre-check (reliable before UNLOCK_POLICIES).
            # CanSlotPolicy returns false when the policy is already in that exact slot,
            # so we also check alreadyThere to avoid false positives on replace-in-place.
            f"local canSlot_{slot_idx} = pCulture:CanSlotPolicy(pe_{slot_idx}.Index, {slot_idx}); "
            f"local alreadyThere_{slot_idx} = (pCulture:GetSlotPolicy({slot_idx}) == pe_{slot_idx}.Index); "
            f"if not canSlot_{slot_idx} and not alreadyThere_{slot_idx} then "
            f'local pType_{slot_idx} = pe_{slot_idx}.GovernmentSlotType or "unknown"; '
            f"local st_{slot_idx} = pCulture:GetSlotType({slot_idx}); "
            f'local sName_{slot_idx} = slotNames[st_{slot_idx}] or ("type_" .. st_{slot_idx}); '
            f"{_bail_lua(f''' "ERR:CANNOT_SLOT|{policy_type} (" .. pType_{slot_idx} .. ") rejected for slot {slot_idx} (" .. sName_{slot_idx} .. ")" ''')} end; "
            # Belt-and-suspenders type string check, now covers Economic/Military/Diplomatic (sType < 3)
            f"local sType_{slot_idx} = pCulture:GetSlotType({slot_idx}); "
            f"local pSlot_{slot_idx} = slotTypeMap[pe_{slot_idx}.GovernmentSlotType] or -1; "
            f"if sType_{slot_idx} < 3 and pSlot_{slot_idx} ~= sType_{slot_idx} "
            f"  and pe_{slot_idx}.GovernmentSlotType ~= 'SLOT_WILDCARD' then "
            f'local sName = slotNames[sType_{slot_idx}] or "unknown"; '
            f'local pType = pe_{slot_idx}.GovernmentSlotType or "unknown"; '
            f"{_bail_lua(f''' "ERR:SLOT_MISMATCH|{policy_type} (" .. pType .. ") cannot go in slot {slot_idx} (" .. sName .. ")" ''')} end"
        )
        add_entries.append(f"addList[{slot_idx}] = pe_{slot_idx}.Hash")
        # Readback compares PolicyType STRING (GetSlotPolicy 返回 index 还是
        # hash 在不同上下文语义不同,比较字符串最稳)。
        readback_entries.append(
            f"do local cur_{slot_idx} = pCulture:GetSlotPolicy({slot_idx}); "
            f"local curType_{slot_idx} = ''; "
            f"if cur_{slot_idx} and cur_{slot_idx} >= 0 then "
            f"local curP_{slot_idx} = GameInfo.Policies[cur_{slot_idx}]; "
            f"if curP_{slot_idx} then curType_{slot_idx} = curP_{slot_idx}.PolicyType end end; "
            f"if curType_{slot_idx} ~= '{policy_type}' then "
            f"table.insert(missing, '{policy_type}@{slot_idx}') end end"
        )

    pre_lua = "; ".join(pre_checks)
    add_lua = "; ".join(add_entries)
    clear_lua = "; ".join(clear_entries)
    readback_lua = "; ".join(readback_entries)

    return f"""
local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()
local numSlots = pCulture:GetNumPolicySlots()
if numSlots <= 0 then {_bail("ERR:NO_GOVERNMENT|No government selected")} end
local slotNames = {{[0]="Economic", [1]="Military", [2]="Diplomatic", [3]="Wildcard", [4]="Wildcard"}}
local slotTypeMap = {{SLOT_ECONOMIC=0, SLOT_MILITARY=1, SLOT_DIPLOMATIC=2, SLOT_WILDCARD=3, SLOT_GREAT_PERSON=4}}
-- 缺陷61(2026-08-09):「换卡被回合结算回滚」不是引擎层限制,是解锁前置
-- 没被满足。vanilla 规则:政策重排只在【市政完成的当回合】免费,其余时间
-- 要花金解锁。T193-T223 十轮"全部被抹"都发生在窗口关着的时候;而 T7 空槽
-- 填卡存活,恰因当回合法典研完=窗口开着。
-- 2026-08-11 T305 二次订正(缺陷104):上面那条"该方法在这个上下文里根本
-- 不存在"是【错的】。实测 InGame 下 GetCostToUnlockPolicies、PolicyChangeMade、
-- IsPolicyObsolete、IsPolicyUnlocked 全部存在可调。T280 那次 pcall 静默失败
-- 多半是在 GameCore 上下文探的 —— CanSlotPolicy 在 GameCore 确实不存在。
-- 于是整套"窗口关着=会被回滚"的叙事建立在一个错误前提上,而真正的失败
-- 原因是过时卡(见上面 pre-check)。T304 六张进三张、T305 三张进零张,
-- 分界线不是窗口而是 IsPolicyObsolete,无一例外。
-- 现在成本实读:cost==0 就是免费,不必猜窗口。
local freeWindow = false
pcall(function() freeWindow = pCulture:CivicCompletedThisTurn() end)
local unlockCost = -1
pcall(function() unlockCost = pCulture:GetCostToUnlockPolicies() end)
{pre_lua}
UI.RequestPlayerOperation(me, PlayerOperations.UNLOCK_POLICIES, {{}})
local clearList = {{}}
{clear_lua}
local addList = {{}}
{add_lua}
pCulture:RequestPolicyChanges(clearList, addList)
-- readback:RequestPolicyChanges 是异步请求,同帧读到旧值只说明【尚未
-- 结算】,不能断言失败(2026-08-09 T7 实测:同帧报 missing,下一帧卡已
-- 在槽 —— 旧文案把成功说成"引擎回滚",误导调用方放弃)。
local missing = {{}}
{readback_lua}
if #missing == 0 then
    print("OK:POLICIES_SET|All {touched} touched slot(s) verified after set.")
elseif freeWindow or unlockCost == 0 then
    -- 免费(市政当回合)或实读成本为 0 —— 这就是个正常的异步请求,同帧读到
    -- 旧值不代表失败。过时卡已经在 pre-check 拦死,走到这里的都还有戏。
    print("NOTE:POLICIES_SUBMITTED|已提交(" .. (freeWindow and "市政完成当回合免费" or ("实读解锁成本 " .. unlockCost)) .. ");同帧读回仍显示: " .. table.concat(missing, ", ") .. " —— 请求式 API 本来就下一帧才结算。下回合用 get_policies 复核。")
else
    -- 成本 > 0:要付费解锁。请求照发,但金库不够时引擎会丢弃。
    print("WARN:POLICIES_NEED_GOLD|换卡需付费解锁,实读成本 " .. unlockCost .. " 金。请求已发,金不够会被丢弃: " .. table.concat(missing, ", ") .. "。下回合用 get_policies 复核;想免费换就等下一个市政完成的那一回合。")
end
print("{SENTINEL}")
"""


def build_governors_query() -> str:
    """Read governor status, appointed governors, and available types (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pGovs = nil
pcall(function() pGovs = Players[me]:GetGovernors() end)
if pGovs == nil then print("ERR:NOT_AVAILABLE|Governors require the Rise & Fall expansion (this game uses the Standard ruleset)"); print("{SENTINEL}"); return end
local pts = pGovs:GetGovernorPoints()
local spent = pGovs:GetGovernorPointsSpent()
local canAppoint = pGovs:CanAppoint() and "1" or "0"
print("STATUS|" .. pts .. "|" .. spent .. "|" .. canAppoint)
local appointedTypes = {}
for row in GameInfo.Governors() do
    if row.TransitionStrength and row.TransitionStrength > 0 and pGovs:HasGovernor(row.Hash) then
        appointedTypes[row.GovernorType] = true
        local g = pGovs:GetGovernor(row.Hash)
        local gName = Locale.Lookup(row.Name)
        local gTitle = Locale.Lookup(row.Title)
        local cityID = -1
        local cityName = "Unassigned"
        local established = "0"
        local turnsLeft = 0
        local assignedCity = g:GetAssignedCity()
        if assignedCity then
            cityID = assignedCity:GetID()
            cityName = Locale.Lookup(assignedCity:GetName())
            established = g:IsEstablished() and "1" or "0"
            if not g:IsEstablished() then turnsLeft = g:GetTurnsToEstablish() end
        end
        print("APPOINTED|" .. row.GovernorType .. "|" .. gName:gsub("|","/") .. "|" .. gTitle:gsub("|","/") .. "|" .. cityID .. "|" .. cityName:gsub("|","/") .. "|" .. established .. "|" .. turnsLeft)
        for promo in GameInfo.GovernorPromotionSets() do
            if promo.GovernorType == row.GovernorType then
                local promoRow = GameInfo.GovernorPromotions[promo.GovernorPromotion]
                if promoRow and not g:HasPromotion(promoRow.Index) then
                    local pName = Locale.Lookup(promoRow.Name)
                    local pDesc = Locale.Lookup(promoRow.Description):gsub("|", "/"):gsub("\\n", " ")
                    local lvl = promoRow.Level or 0
                    local col = promoRow.Column or 0
                    print("GOV_PROMO|" .. row.GovernorType .. "|" .. promoRow.GovernorPromotionType .. "|" .. pName:gsub("|","/") .. "|" .. pDesc .. "|" .. lvl .. "|" .. col)
                end
            end
        end
    end
end
for gov in GameInfo.Governors() do
    if gov.TransitionStrength and gov.TransitionStrength > 0 and not appointedTypes[gov.GovernorType] then
        local gName = Locale.Lookup(gov.Name)
        local gTitle = Locale.Lookup(gov.Title)
        local gDesc = gov.Description and Locale.Lookup(gov.Description):gsub("|", "/"):gsub("\\n", " ") or ""
        print("AVAILABLE|" .. gov.GovernorType .. "|" .. gName:gsub("|","/") .. "|" .. gTitle:gsub("|","/") .. "|" .. gDesc)
        for promo in GameInfo.GovernorPromotionSets() do
            if promo.GovernorType == gov.GovernorType then
                local promoRow = GameInfo.GovernorPromotions[promo.GovernorPromotion]
                if promoRow then
                    local pName = Locale.Lookup(promoRow.Name)
                    local pDesc = Locale.Lookup(promoRow.Description):gsub("|", "/"):gsub("\\n", " ")
                    local lvl = promoRow.Level or 0
                    local col = promoRow.Column or 0
                    print("GOV_PROMO|" .. gov.GovernorType .. "|" .. promoRow.GovernorPromotionType .. "|" .. pName:gsub("|","/") .. "|" .. pDesc .. "|" .. lvl .. "|" .. col)
                end
            end
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_appoint_governor(governor_type: str) -> str:
    """Appoint a new governor (InGame context)."""
    return f"""
local me = Game.GetLocalPlayer()
local pGovs = nil
pcall(function() pGovs = Players[me]:GetGovernors() end)
if pGovs == nil then {_bail("ERR:NOT_AVAILABLE|Governors require the Rise & Fall expansion (this game uses the Standard ruleset)")} end
if not pGovs:CanAppoint() then {_bail("ERR:CANNOT_APPOINT|No governor points available")} end
local gov = GameInfo.Governors["{governor_type}"]
if gov == nil then {_bail(f"ERR:GOVERNOR_NOT_FOUND|{governor_type}")} end
if PlayerOperations.APPOINT_GOVERNOR == nil then {_bail("ERR:API_MISSING|PlayerOperations.APPOINT_GOVERNOR is nil")} end
if PlayerOperations.PARAM_GOVERNOR_TYPE == nil then {_bail("ERR:API_MISSING|PlayerOperations.PARAM_GOVERNOR_TYPE is nil")} end
local prePts = pGovs:GetGovernorPointsSpent()
local params = {{}}
params[PlayerOperations.PARAM_GOVERNOR_TYPE] = gov.Index
UI.RequestPlayerOperation(me, PlayerOperations.APPOINT_GOVERNOR, params)
local postPts = pGovs:GetGovernorPointsSpent()
if postPts > prePts then
    print("OK:APPOINTED|" .. Locale.Lookup(gov.Name) .. " (" .. Locale.Lookup(gov.Title) .. ")")
else
    print("OK:APPOINT_REQUESTED|" .. Locale.Lookup(gov.Name) .. " — verify with get_governors()")
end
print("{SENTINEL}")
"""


def build_assign_governor(governor_type: str, city_id: int) -> str:
    """Assign a governor to a city (InGame context)."""
    return f"""
{_lua_get_city(city_id)}
local gov = GameInfo.Governors["{governor_type}"]
if gov == nil then {_bail(f"ERR:GOVERNOR_NOT_FOUND|{governor_type}")} end
if PlayerOperations.ASSIGN_GOVERNOR == nil then {_bail("ERR:API_MISSING|PlayerOperations.ASSIGN_GOVERNOR is nil")} end
local params = {{}}
params[PlayerOperations.PARAM_GOVERNOR_TYPE] = gov.Index
params[PlayerOperations.PARAM_CITY_DEST] = pCity:GetID()
params[PlayerOperations.PARAM_PLAYER_ONE] = me
UI.RequestPlayerOperation(me, PlayerOperations.ASSIGN_GOVERNOR, params)
print("OK:ASSIGNED|" .. Locale.Lookup(gov.Name) .. " to " .. Locale.Lookup(pCity:GetName()))
print("{SENTINEL}")
"""


def build_promote_governor(governor_type: str, promotion_type: str) -> str:
    """Promote a governor with a new ability (InGame context).

    Uses PROMOTE_GOVERNOR operation (NOT APPOINT_GOVERNOR).
    Both governor and promotion use .Index (NOT .Hash).
    Source: GovernorDetailsPanel.lua — SetVoid1(m_GovernorIndex), SetVoid2(kPromotion.Index)
    """
    return f"""
local me = Game.GetLocalPlayer()
local pGovs = nil
pcall(function() pGovs = Players[me]:GetGovernors() end)
if pGovs == nil then {_bail("ERR:NOT_AVAILABLE|Governors require the Rise & Fall expansion (this game uses the Standard ruleset)")} end
local gov = GameInfo.Governors["{governor_type}"]
if gov == nil then {_bail(f"ERR:GOVERNOR_NOT_FOUND|{governor_type}")} end
if not pGovs:HasGovernor(gov.Hash) then {_bail(f"ERR:NOT_APPOINTED|{governor_type} not appointed")} end
local promo = GameInfo.GovernorPromotions["{promotion_type}"]
if promo == nil then {_bail(f"ERR:PROMOTION_NOT_FOUND|{promotion_type}")} end
local g = pGovs:GetGovernor(gov.Hash)
-- Check HasPromotion BEFORE CanPromoteGovernor so the agent gets
-- a specific "already earned" error instead of a generic "can't promote"
if g:HasPromotion(promo.Index) then {_bail(f"ERR:ALREADY_PROMOTED|{promotion_type} already earned")} end
local pts = pGovs:GetGovernorPoints() - pGovs:GetGovernorPointsSpent()
if pts <= 0 then {_bail("ERR:CANNOT_PROMOTE|No governor points available (0 remaining)")} end
if not pGovs:CanPromoteGovernor(gov.Hash) then {_bail("ERR:CANNOT_PROMOTE|Governor has no available promotions")} end
-- Check prerequisites (OR-based: need at least one satisfied)
local prereqs = {{}}
for row in GameInfo.GovernorPromotionPrereqs() do
    if row.GovernorPromotionType == "{promotion_type}" then
        table.insert(prereqs, row.PrereqGovernorPromotion)
    end
end
if #prereqs > 0 then
    local anyMet = false
    local names = {{}}
    for _, reqType in ipairs(prereqs) do
        local reqRow = GameInfo.GovernorPromotions[reqType]
        if reqRow and g:HasPromotion(reqRow.Index) then anyMet = true; break end
        if reqRow then table.insert(names, Locale.Lookup(reqRow.Name)) end
    end
    if not anyMet then print("ERR:PREREQ_NOT_MET|{promotion_type} requires one of: " .. table.concat(names, ", ")); print("{SENTINEL}"); return end
end
local params = {{}}
params[PlayerOperations.PARAM_GOVERNOR_TYPE] = gov.Index
params[PlayerOperations.PARAM_GOVERNOR_PROMOTION_TYPE] = promo.Index
UI.RequestPlayerOperation(me, PlayerOperations.PROMOTE_GOVERNOR, params)
print("OK:PROMOTED|" .. Locale.Lookup(gov.Name) .. " with " .. Locale.Lookup(promo.Name))
print("{SENTINEL}")
"""


def build_unit_promotions_query(unit_index: int) -> str:
    """List available promotions for a unit (GameCore context)."""
    return f"""
{_lua_get_unit_gamecore(unit_index)}
local x, y = unit:GetX(), unit:GetY()
if x == -9999 then {_bail("ERR:UNIT_CONSUMED")} end
local typeIdx = unit:GetType()
if typeIdx == nil then {_bail("ERR:UNIT_NO_TYPE")} end
local info = GameInfo.Units[typeIdx]
local ut = info and info.UnitType or "UNKNOWN"
local promClass = info and info.PromotionClass or ""
print("UNIT|" .. {unit_index} .. "|" .. (unit:GetID() % 65536) .. "|" .. ut)
local exp = unit:GetExperience()
{_LUA_XP_THRESHOLD}
print("XP|" .. xp .. "|" .. xpNeeded .. "|" .. xpPromoCount)
if xp < xpNeeded then
    print("{SENTINEL}")
    return
end
local prereqMap = {{}}
for row in GameInfo.UnitPromotionPrereqs() do
    local pt = row.UnitPromotion
    if not prereqMap[pt] then prereqMap[pt] = {{}} end
    table.insert(prereqMap[pt], row.PrereqUnitPromotion)
end
for promo in GameInfo.UnitPromotions() do
    if promo.PromotionClass == promClass then
        if not exp:HasPromotion(promo.Index) then
            local prereqs = prereqMap[promo.UnitPromotionType]
            local prereqMet = true
            if prereqs and #prereqs > 0 then
                prereqMet = false
                for _, reqType in ipairs(prereqs) do
                    local reqInfo = GameInfo.UnitPromotions[reqType]
                    if reqInfo and exp:HasPromotion(reqInfo.Index) then
                        prereqMet = true
                        break
                    end
                end
            end
            if prereqMet then
                local canPromote = false
                pcall(function() canPromote = exp:CanPromote(promo.Index) end)
                if canPromote then
                    local name = Locale.Lookup(promo.Name)
                    local desc = Locale.Lookup(promo.Description):gsub("|","/"):gsub("\\n"," ")
                    print("PROMO|" .. promo.UnitPromotionType .. "|" .. name:gsub("|","/") .. "|" .. desc)
                end
            end
        end
    end
end
print("{SENTINEL}")
"""


def build_promote_unit(unit_index: int, promotion_type: str) -> str:
    """Apply a promotion to a unit (GameCore context).

    Uses GameCore SetPromotion because InGame RequestCommand(PROMOTE)
    silently fails (persistent bug across Games 1, 4, and 5).
    """
    return f"""
{_lua_get_unit_gamecore(unit_index)}
local x, y = unit:GetX(), unit:GetY()
if x == -9999 then {_bail("ERR:UNIT_CONSUMED")} end
local promo = GameInfo.UnitPromotions["{promotion_type}"]
if promo == nil then {_bail(f"ERR:PROMOTION_NOT_FOUND|{promotion_type}")} end
local exp = unit:GetExperience()
if exp == nil then {_bail("ERR:NO_EXPERIENCE|Unit has no experience object")} end
-- XP-threshold gate: prevent infinite promotions from SetPromotion desync.
local ui = GameInfo.Units[unit:GetType()]
local promClass = ui and ui.PromotionClass or ""
{_LUA_XP_THRESHOLD}
if xp < xpNeeded then {_bail_lua('"ERR:INSUFFICIENT_XP|Have " .. xp .. " XP, need " .. xpNeeded .. " for promotion " .. (xpPromoCount + 1) .. " (have " .. xpPromoCount .. " already)"')} end
local canPromote = false
pcall(function() canPromote = exp:CanPromote(promo.Index) end)
if not canPromote then {_bail("ERR:CANNOT_PROMOTE|Unit cannot receive this promotion (wrong class, missing prereq, or insufficient XP)")} end
if exp:HasPromotion(promo.Index) then {_bail(f"ERR:ALREADY_HAS_PROMOTION|{promotion_type}")} end
exp:SetPromotion(promo.Index)
if not exp:HasPromotion(promo.Index) then {_bail("ERR:PROMOTION_FAILED|SetPromotion did not apply")} end
-- Sync engine state: zero out stored promotions so the engine stops
-- generating ENDTURN_BLOCKING_UNIT_PROMOTION notifications.
-- ChangeStoredPromotions(-1) is insufficient when stored > 1 (e.g. unit
-- earned enough XP for two promotions). Read the current count and zero it.
local stored = 0
pcall(function() stored = exp:GetStoredPromotions() end)
if stored > 0 then
    pcall(function() exp:ChangeStoredPromotions(-stored) end)
end
-- Verify: if stored is still > 0 after zeroing, log it for diagnostics
local storedAfter = 0
pcall(function() storedAfter = exp:GetStoredPromotions() end)
pcall(function() unit:SetDamage(0) end)
local promoName = Locale.Lookup(promo.Name)
print("OK:PROMOTED|" .. promoName .. "|stored:" .. stored .. "->" .. storedAfter)
print("{SENTINEL}")
"""


def build_city_states_query() -> str:
    """List known city-states with envoy info (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pInfluence = Players[me]:GetInfluence()
local pDiplo = Players[me]:GetDiplomacy()
print("TOKENS|" .. pInfluence:GetTokensToGive())
local csTypeMap = {}
csTypeMap["LEADER_MINOR_CIV_SCIENTIFIC"] = "Scientific"
csTypeMap["LEADER_MINOR_CIV_CULTURAL"] = "Cultural"
csTypeMap["LEADER_MINOR_CIV_MILITARISTIC"] = "Militaristic"
csTypeMap["LEADER_MINOR_CIV_RELIGIOUS"] = "Religious"
csTypeMap["LEADER_MINOR_CIV_TRADE"] = "Trade"
csTypeMap["LEADER_MINOR_CIV_INDUSTRIAL"] = "Industrial"
for i = 0, 62 do
    if Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() == false and Players[i]:IsBarbarian() == false and pDiplo:HasMet(i) then
        local cfg = PlayerConfigurations[i]
        local name = Locale.Lookup(cfg:GetPlayerName())
        local leaderType = cfg:GetLeaderTypeName() or ""
        local csType = "Unknown"
        local leader = GameInfo.Leaders[leaderType]
        if leader and leader.InheritFrom then
            csType = csTypeMap[leader.InheritFrom] or leader.InheritFrom
        end
        local csInfluence = Players[i]:GetInfluence()
        local envoys = csInfluence:GetTokensReceived(me)
        local suzID = csInfluence:GetSuzerain() or -1
        local suzName = "None"
        if suzID >= 0 and suzID ~= 63 then
            local sCfg = PlayerConfigurations[suzID]
            if sCfg then suzName = Locale.Lookup(sCfg:GetCivilizationShortDescription()) end
        end
        local canSend = pInfluence:CanGiveTokensToPlayer(i) and "1" or "0"
        print("CS|" .. i .. "|" .. name:gsub("|","/") .. "|" .. csType .. "|" .. envoys .. "|" .. suzID .. "|" .. suzName:gsub("|","/") .. "|" .. canSend)
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_send_envoy(city_state_player_id: int) -> str:
    """Send an envoy to a city-state (InGame context)."""
    return f"""
local me = Game.GetLocalPlayer()
local pInfluence = Players[me]:GetInfluence()
if pInfluence:GetTokensToGive() <= 0 then {_bail("ERR:NO_ENVOYS|No envoy tokens available")} end
if not pInfluence:CanGiveTokensToPlayer({city_state_player_id}) then
    {_bail(f"ERR:CANNOT_SEND|Cannot send envoy to player {city_state_player_id}")}
end
local params = {{}}
params[PlayerOperations.PARAM_PLAYER_ONE] = {city_state_player_id}
params[PlayerOperations.PARAM_FLAGS] = 0
UI.RequestPlayerOperation(me, PlayerOperations.GIVE_INFLUENCE_TOKEN, params)
local cfg = PlayerConfigurations[{city_state_player_id}]
local name = cfg and Locale.Lookup(cfg:GetPlayerName()) or "Unknown"
print("OK:ENVOY_SENT|" .. name)
print("{SENTINEL}")
"""


def build_unit_upgrade_query(unit_index: int) -> str:
    """Check if a unit can upgrade and get cost/target info (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
{_LUA_RESOLVE_UU}
local info = GameInfo.Units[unit:GetType()]
local ut = info and info.UnitType or "UNKNOWN"
local params = {{}}
local canUpgrade, upgradeResult = UnitManager.CanStartCommand(unit, UnitCommandTypes.UPGRADE, params, true)
local upgCol = info.UpgradeUnitCollection
local upgradeType = ""
if upgCol and #upgCol > 0 then upgradeType = upgCol[1].UpgradeUnit or "" end
-- 0c fix: 升级目标映射为本文明 UU(如罗马 勇士→军团,而非需铁的剑士)
upgradeType = resolveUU(upgradeType)
if not canUpgrade then
    local reasons = ""
    if upgradeResult then
        for _, v in pairs(upgradeResult) do
            if type(v) == "table" then
                for _, reason in pairs(v) do
                    if type(reason) == "string" then
                        local clean = reason:gsub("%[ICON_[^%]]*%]", ""):gsub("%s+", " ")
                        reasons = reasons .. (reasons ~= "" and "; " or "") .. clean
                    end
                end
            end
        end
    end
    local suffix = upgradeType ~= "" and (" -> " .. upgradeType) or ""
    local msg = ut .. suffix .. (reasons ~= "" and (" | " .. reasons) or " | cannot upgrade (missing tech, resources, gold, or no path)")
    {_bail_lua('"ERR:CANNOT_UPGRADE|" .. msg')}
end
if upgradeType == "" then {_bail_lua('"ERR:NO_UPGRADE_PATH|" .. ut .. " has no upgrade"')} end
local upInfo = GameInfo.Units[upgradeType]
local upName = upInfo and Locale.Lookup(upInfo.Name) or upgradeType
local gold = Players[me]:GetTreasury():GetGoldBalance()
local cost = 0
pcall(function() cost = unit:GetUpgradeCost() end)
print("UPGRADE|" .. ut .. "|" .. upgradeType .. "|" .. upName:gsub("|","/") .. "|" .. math.floor(cost) .. "|" .. math.floor(gold))
print("{SENTINEL}")
"""


def build_upgrade_unit(unit_index: int) -> str:
    """Execute unit upgrade (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
{_LUA_RESOLVE_UU}
local info = GameInfo.Units[unit:GetType()]
local ut = info and info.UnitType or "UNKNOWN"
local upgCol = info and info.UpgradeUnitCollection
local upType = (upgCol and #upgCol > 0) and upgCol[1].UpgradeUnit or ""
-- 0c fix: 报告实际会升出的单位(UU 替换后),而不是基础单位
upType = resolveUU(upType)
local params = {{}}
local canUpgrade, upgradeResult = UnitManager.CanStartCommand(unit, UnitCommandTypes.UPGRADE, params, true)
if not canUpgrade then
    local cost = 0
    pcall(function() cost = unit:GetUpgradeCost() end)
    local gold = Players[me]:GetTreasury():GetGoldBalance()
    local detail = ut
    if upType ~= "" then detail = detail .. " -> " .. upType end
    detail = detail .. " | cost:" .. math.floor(cost) .. "g have:" .. math.floor(gold) .. "g"
    if upgradeResult then
        for _, v in pairs(upgradeResult) do
            if type(v) == "table" then
                for _, reason in pairs(v) do
                    if type(reason) == "string" then
                        local clean = reason:gsub("%[ICON_[^%]]*%]", ""):gsub("%s+", " ")
                        detail = detail .. " | " .. clean
                    end
                end
            end
        end
    end
    {_bail_lua('"ERR:CANNOT_UPGRADE|" .. detail')}
end
if upType == "" then upType = "UNKNOWN" end
local ux, uy = unit:GetX(), unit:GetY()
UnitManager.RequestCommand(unit, UnitCommandTypes.UPGRADE, params)
-- 2026-08-10 罗马局 T185:升级会【换掉单位对象】—— 旧的复合 ID 立刻失效,
-- 拿它下一条指令直接 UNIT_NOT_FOUND(我在前线正要指挥它西进时踩到)。
-- 升级后按坐标找回新单位,把新 ID 一起返回,调用方不必再拉一次 get_units。
local newId = nil
pcall(function()
    for _, nu in Players[me]:GetUnits():Members() do
        if nu:GetX() == ux and nu:GetY() == uy then
            local ni = GameInfo.Units[nu:GetType()]
            if ni and ni.UnitType == upType then newId = nu:GetID(); break end
        end
    end
end)
local line = "OK:UPGRADED|" .. ut .. " -> " .. upType
if newId ~= nil then
    line = line .. "|new_unit_id:" .. newId .. "(旧 ID 已失效,后续指令用这个)"
else
    line = line .. "|new_unit_id:UNKNOWN(旧 ID 已失效 —— 先 get_units 再指挥它)"
end
print(line)
print("{SENTINEL}")
"""


def build_dedications_query() -> str:
    """Read current era age, available dedications, and active ones."""
    return """
local me = Game.GetLocalPlayer()
local pEras = nil
pcall(function() pEras = Game.GetEras() end)
if pEras == nil then print("ERR:NOT_AVAILABLE|Dedications/Ages require the Rise & Fall expansion (this game uses the Standard ruleset)"); print("{SENTINEL}"); return end
local age = "Normal"
if pEras:HasHeroicGoldenAge(me) then age = "Heroic"
elseif pEras:HasGoldenAge(me) then age = "Golden"
elseif pEras:HasDarkAge(me) then age = "Dark" end
local era = pEras:GetCurrentEra()
local darkT = pEras:GetPlayerDarkAgeThreshold(me) or 0
local goldT = pEras:GetPlayerGoldenAgeThreshold(me) or 0
local allowed = pEras:GetPlayerNumAllowedCommemorations(me)
local score = pEras:GetPlayerCurrentScore(me)
print("STATUS|" .. age .. "|" .. era .. "|" .. score .. "|" .. darkT .. "|" .. goldT .. "|" .. allowed)
-- Active commemorations
local active = pEras:GetPlayerActiveCommemorations(me)
if active then
    for _, a in ipairs(active) do
        local row = GameInfo.CommemorationTypes[a]
        if row then print("ACTIVE|" .. row.CommemorationType) end
    end
end
-- Available choices
local choices = pEras:GetPlayerCommemorateChoices(me)
if choices then
    for _, idx in ipairs(choices) do
        local row = GameInfo.CommemorationTypes[idx]
        if row then
            local norm = row.NormalAgeBonusDescription and Locale.Lookup(row.NormalAgeBonusDescription) or ""
            local gold = row.GoldenAgeBonusDescription and Locale.Lookup(row.GoldenAgeBonusDescription) or ""
            local dark = row.DarkAgeBonusDescription and Locale.Lookup(row.DarkAgeBonusDescription) or ""
            print("CHOICE|" .. idx .. "|" .. row.CommemorationType .. "|" .. norm .. "|" .. gold .. "|" .. dark)
        end
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_choose_dedication(dedication_index: int) -> str:
    """Select a dedication/commemoration by its index."""
    return f"""
local me = Game.GetLocalPlayer()
local pEras = nil
pcall(function() pEras = Game.GetEras() end)
if pEras == nil then {_bail("ERR:NOT_AVAILABLE|Dedications/Ages require the Rise & Fall expansion (this game uses the Standard ruleset)")} end
local allowed = pEras:GetPlayerNumAllowedCommemorations(me)
if allowed <= 0 then {_bail("ERR:NO_DEDICATION_NEEDED|No dedication selection required (already chosen or not available)")} end
local row = GameInfo.CommemorationTypes[{dedication_index}]
if not row then {_bail(f"ERR:INVALID_INDEX|Dedication index {dedication_index} not found")} end
local params = {{}}
params[PlayerOperations.PARAM_COMMEMORATION_TYPE] = {dedication_index}
UI.RequestPlayerOperation(me, PlayerOperations.COMMEMORATE, params)
print("OK:DEDICATION_CHOSEN|" .. row.CommemorationType)
print("{SENTINEL}")
"""


def build_available_governments_query() -> str:
    """List unlocked governments with slot info (InGame context)."""
    return """
local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()
local curGov = pCulture:GetCurrentGovernment()
for row in GameInfo.Governments() do
    local unlocked = pCulture:IsGovernmentUnlocked(row.Index)
    if unlocked then
        local isCurrent = (row.Index == curGov)
        local slots = {}
        for slotRow in GameInfo.Government_SlotCounts() do
            if slotRow.GovernmentType == row.GovernmentType then
                for i = 1, slotRow.NumSlots do
                    table.insert(slots, slotRow.GovernmentSlotType)
                end
            end
        end
        local slotStr = table.concat(slots, ",")
        local name = Locale.Lookup(row.Name)
        local bonus = ""
        if row.BonusType then
            local bRow = GameInfo.GovernmentBonuses and GameInfo.GovernmentBonuses[row.BonusType]
            if bRow then bonus = Locale.Lookup(bRow.Description or "") end
        end
        local tag = isCurrent and "CURRENT" or "AVAILABLE"
        print("GOV|" .. row.GovernmentType .. "|" .. row.Index .. "|" .. tag .. "|" .. name .. "|" .. slotStr .. "|" .. bonus)
    end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_change_government(gov_type: str) -> str:
    """Switch government type (InGame context)."""
    return f"""
local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()
local row = GameInfo.Governments["{gov_type}"]
if row == nil then {_bail(f"ERR:GOV_NOT_FOUND|{gov_type}")} end
if not pCulture:IsGovernmentUnlocked(row.Index) then {_bail(f"ERR:GOV_LOCKED|{gov_type} is not unlocked")} end
if row.Index == pCulture:GetCurrentGovernment() then {_bail(f"ERR:ALREADY_CURRENT|{gov_type} is already your government")} end
-- 2026-08-07 P0 实机(苏美尔局 T116→T118):此前这里到 RequestChangeGovernment
-- 就结束了,报 OK:GOVERNMENT_CHANGED。两回合后政体仍是酋邦。
--
-- Civ6 里换政体和排卡是【同一个待确认变更集】:
--   RequestChangeGovernment  = 把政体变更放进待定集(仅"考虑")
--   RequestPolicyChanges     = 提交整个待定集
-- 只"考虑"不提交,回合结算时就丢弃了。更坏的是同帧 GetNumPolicySlots()
-- 会返回【考虑中】那个政体的槽位数 —— 于是 get_policies 当场显示
-- "古典共和,4 个槽",看起来完全成功。
--
-- 而且紧跟着调 set_policies 会雪上加霜:它开头的 UNLOCK_POLICIES 会
-- 重置待定集,把政体变更冲掉,然后把 4 张卡提交到一个只有 2 槽的政体上
-- —— 一次调用报成功,另一次调用把它悄悄撤销,两次都不报错。
--
-- 修法:自己提交(空的 clear/add 表 = 只提交政体变更),然后 readback。
pCulture:SetGovernmentChangeConsidered(true)
pCulture:RequestChangeGovernment(row.Index)
local okCommit = pcall(function() pCulture:RequestPolicyChanges({{}}, {{}}) end)
local nowGov = -1
pcall(function() nowGov = pCulture:GetCurrentGovernment() end)
if nowGov == row.Index then
    print("OK:GOVERNMENT_CHANGED|{gov_type}|" .. Locale.Lookup(row.Name) .. "|committed+verified")
elseif okCommit then
    -- 提交调用没抛,但同帧还没结算。诚实报 PENDING —— 上一版就是在这里
    -- 谎报成功的。附带顺序警告:本回合再调 set_policies 会把它冲掉。
    print("NOTE:GOVERNMENT_PENDING|{gov_type}|已提交但同帧未结算。⚠️ 本回合【不要】再调 set_policies —— 它会重置待定集并撤销本次政体变更。先 end_turn,下回合用 get_policies 复核政体已变,再排卡。")
else
    {_bail(f"ERR:GOVERNMENT_COMMIT_FAILED|{gov_type} 的变更请求提交失败(RequestPolicyChanges 抛错)。政体未改变。")}
end
print("{SENTINEL}")
"""


def parse_policies_response(lines: list[str]) -> GovernmentStatus:
    """Parse GOV|, SLOT|, AVAIL| lines from build_policies_query."""
    gov_name = "None"
    gov_type = "NONE"
    slots: list[PolicySlot] = []
    available: list[PolicyInfo] = []

    for line in lines:
        if line.startswith("GOV|"):
            parts = line.split("|")
            if len(parts) >= 4:
                gov_type = parts[1]
                gov_name = parts[2]
        elif line.startswith("SLOT|"):
            parts = line.split("|")
            if len(parts) >= 5:
                policy_type = None if parts[3] == "NONE" else parts[3]
                policy_name = None if parts[4] == "Empty" else parts[4]
                slots.append(
                    PolicySlot(
                        slot_index=int(parts[1]),
                        slot_type=parts[2],
                        current_policy=policy_type,
                        current_policy_name=policy_name,
                    )
                )
        elif line.startswith("AVAIL|"):
            parts = line.split("|")
            if len(parts) >= 5:
                available.append(
                    PolicyInfo(
                        policy_type=parts[1],
                        name=parts[2],
                        description=parts[3],
                        slot_type=parts[4],
                    )
                )

    return GovernmentStatus(
        government_name=gov_name,
        government_type=gov_type,
        slots=slots,
        available_policies=available,
    )


def parse_governors_response(lines: list[str]) -> GovernorStatus:
    """Parse STATUS|, APPOINTED|, GOV_PROMO|, AVAILABLE| lines from build_governors_query."""
    pts_avail = 0
    pts_spent = 0
    can_appoint = False
    appointed: list[AppointedGovernor] = []
    available: list[GovernorInfo] = []
    # Collect promotions keyed by governor_type, then attach after
    promos_by_gov: dict[str, list[GovernorPromotion]] = {}

    for line in lines:
        if line.startswith("STATUS|"):
            parts = line.split("|")
            if len(parts) >= 4:
                pts_total = int(parts[1])
                pts_spent = int(parts[2])
                pts_avail = pts_total - pts_spent
                can_appoint = parts[3] == "1"
        elif line.startswith("APPOINTED|"):
            parts = line.split("|")
            if len(parts) >= 7:
                appointed.append(
                    AppointedGovernor(
                        governor_type=parts[1],
                        name=parts[2],
                        assigned_city_id=int(parts[4]),
                        assigned_city_name=parts[5],
                        is_established=parts[6] == "1",
                        turns_to_establish=int(parts[7]) if len(parts) >= 8 else 0,
                    )
                )
        elif line.startswith("GOV_PROMO|"):
            parts = line.split("|")
            if len(parts) >= 5:
                gov_type = parts[1]
                promos_by_gov.setdefault(gov_type, []).append(
                    GovernorPromotion(
                        promotion_type=parts[2],
                        name=parts[3],
                        description=parts[4],
                        level=int(parts[5]) if len(parts) > 5 else 0,
                        column=int(parts[6]) if len(parts) > 6 else 0,
                    )
                )
        elif line.startswith("AVAILABLE|"):
            parts = line.split("|")
            if len(parts) >= 4:
                available.append(
                    GovernorInfo(
                        governor_type=parts[1],
                        name=parts[2],
                        title=parts[3],
                        description=parts[4] if len(parts) > 4 else "",
                    )
                )

    # Attach promotions to their governors
    for gov in appointed:
        gov.available_promotions = promos_by_gov.get(gov.governor_type, [])

    # Attach promotions to available governors, split out base ability
    for gov in available:
        all_promos = promos_by_gov.get(gov.governor_type, [])
        for p in all_promos:
            if p.level == 0:
                gov.base_ability = p.name
                gov.base_ability_desc = p.description
            else:
                gov.promotions.append(p)

    return GovernorStatus(
        points_available=pts_avail,
        points_spent=pts_spent,
        can_appoint=can_appoint,
        appointed=appointed,
        available_to_appoint=available,
    )


def parse_unit_promotions_response(lines: list[str]) -> UnitPromotionStatus:
    """Parse UNIT|, XP|, and PROMO| lines from build_unit_promotions_query."""
    unit_id = 0
    unit_index = 0
    unit_type = "UNKNOWN"
    promotions: list[PromotionOption] = []
    xp = 0
    xp_needed = 0
    promotion_count = 0

    for line in lines:
        if line.startswith("ERR:"):
            raise ValueError(line[4:])
        if line.startswith("UNIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                unit_id = int(parts[1])
                unit_index = int(parts[2])
                unit_type = parts[3]
        elif line.startswith("XP|"):
            parts = line.split("|")
            if len(parts) >= 4:
                xp = _int(parts[1])
                xp_needed = _int(parts[2])
                promotion_count = int(parts[3])
        elif line.startswith("PROMO|"):
            parts = line.split("|")
            if len(parts) >= 4:
                promotions.append(
                    PromotionOption(
                        promotion_type=parts[1],
                        name=parts[2],
                        description=parts[3],
                    )
                )

    return UnitPromotionStatus(
        unit_id=unit_id,
        unit_index=unit_index,
        unit_type=unit_type,
        promotions=promotions,
        xp=xp,
        xp_needed=xp_needed,
        promotion_count=promotion_count,
    )


def parse_city_states_response(lines: list[str]) -> EnvoyStatus:
    """Parse TOKENS| and CS| lines from build_city_states_query."""
    tokens = 0
    city_states: list[CityStateInfo] = []

    for line in lines:
        if line.startswith("TOKENS|"):
            tokens = int(line.split("|")[1])
        elif line.startswith("CS|"):
            parts = line.split("|")
            if len(parts) >= 8:
                city_states.append(
                    CityStateInfo(
                        player_id=int(parts[1]),
                        name=parts[2],
                        city_state_type=parts[3],
                        envoys_sent=int(parts[4]),
                        suzerain_id=int(parts[5]),
                        suzerain_name=parts[6],
                        can_send_envoy=parts[7] == "1",
                    )
                )

    return EnvoyStatus(tokens_available=tokens, city_states=city_states)


def parse_dedications_response(lines: list[str]) -> DedicationStatus:
    """Parse STATUS|, ACTIVE|, and CHOICE| lines from build_dedications_query."""
    age_type = "Normal"
    era = 0
    era_score = 0
    dark_threshold = 0
    golden_threshold = 0
    selections_allowed = 0
    active: list[str] = []
    choices: list[DedicationChoice] = []

    for line in lines:
        if line.startswith("STATUS|"):
            parts = line.split("|")
            if len(parts) >= 7:
                age_type = parts[1]
                era = int(parts[2])
                era_score = int(parts[3])
                dark_threshold = int(parts[4])
                golden_threshold = int(parts[5])
                selections_allowed = int(parts[6])
        elif line.startswith("ACTIVE|"):
            active.append(line.split("|", 1)[1])
        elif line.startswith("CHOICE|"):
            parts = line.split("|", 5)
            if len(parts) >= 6:
                choices.append(
                    DedicationChoice(
                        index=int(parts[1]),
                        name=parts[2],
                        normal_desc=parts[3],
                        golden_desc=parts[4],
                        dark_desc=parts[5],
                    )
                )

    return DedicationStatus(
        age_type=age_type,
        era=era,
        era_score=era_score,
        dark_threshold=dark_threshold,
        golden_threshold=golden_threshold,
        selections_allowed=selections_allowed,
        active=active,
        choices=choices,
    )
