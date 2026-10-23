"""Units domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import (
    _LUA_HEX_DIST,
    _LUA_MELEE_DOMAIN,
    _LUA_RES_VISIBLE,
    _LUA_RESOLVE_UU,
    SENTINEL,
    _bail,
    _bail_lua,
    _lua_get_unit,
    _lua_get_unit_gamecore,
)
from civ_mcp.lua.models import (
    BuilderInfo,
    BuilderTask,
    CombatEstimate,
    PathingEstimate,
    ThreatInfo,
    UnitInfo,
)


def build_units_query() -> str:
    """InGame context: lists all units with upgrade and builder improvement info."""
    return (
        _LUA_HEX_DIST
        + "\n"
        + _LUA_MELEE_DOMAIN
        + """
local id = Game.GetLocalPlayer()
"""
        + _LUA_RESOLVE_UU
        + """
for i, u in Players[id]:GetUnits():Members() do
    local x, y = u:GetX(), u:GetY()
    if x ~= -9999 then
        local uid = u:GetID()
        local entry = GameInfo.Units[u:GetType()]
        local ut = entry and entry.UnitType or "UNKNOWN"
        local nm = Locale.Lookup(u:GetName())
        local cs = entry and entry.Combat or 0
        -- 攻城类(投石机/攻城炮/大炮)的远程战力在【Bombard】列,RangedCombat 是 0。
        -- 2026-08-11 T291 实测:UNIT_ARTILLERY Combat=60 RangedCombat=0 Bombard=80,
        -- 而 UNIT_FIELD_CANNON 是 RangedCombat=60 Bombard=0。只读 RangedCombat 的话,
        -- 整个攻城兵种线的主武器读成 0 —— 列表里不显示 RS、攻击时被当成近战、
        -- 白吃反击,而攻城单位恰恰是啃城墙唯一高效的手段。
        local rs = math.max(
            tonumber(entry and entry.RangedCombat) or 0,
            tonumber(entry and entry.Bombard) or 0)
        local charges = u:GetBuildCharges() or 0
        local gp = u:GetGreatPerson()
        if gp then
            local ok_gp, gp_charges = pcall(function() return gp:GetActionCharges() end)
            if ok_gp and gp_charges and gp_charges > 0 then charges = gp_charges end
            if charges == 0 then
                -- Cultural GPs (Writers/Artists/Musicians) return 0 from
                -- GetActionCharges(). Fall back to the individual definition.
                pcall(function()
                    local indIdx = gp:GetIndividual()
                    for ind in GameInfo.GreatPersonIndividuals() do
                        if ind.Index == indIdx then
                            charges = ind.ActionCharges or 0
                            break
                        end
                    end
                end)
            end
        end
        if charges == 0 then
            local ok_sp, sp = pcall(function() return u:GetSpreadCharges() end)
            if ok_sp and sp and sp > 0 then charges = sp end
        end
        local relName = ""
        local ok_r, rIdx = pcall(function() return u:GetReligionType() end)
        if ok_r and rIdx and rIdx >= 0 then
            for row in GameInfo.Religions() do
                if row.Index == rIdx then relName = row.ReligionType; break end
            end
        end
        -- Scan for attackable enemies if unit has moves
        local targets = ""
        if u:GetMovesRemaining() > 0 and (cs > 0 or rs > 0) then
            local rng = (rs > 0) and (entry and entry.Range or 1) or 1
            local tgtList = {}
            for dy = -rng, rng do
                for dx = -rng, rng do
                    local tx, ty = x + dx, y + dy
                    -- 2026-08-06:同铁律,距离用 hexDist。这里误报会让
                    -- 本可攻击的敌人不出现在 targets 里(表现为"看起来打不了")。
                    local d = hexDist(x, y, tx, ty)
                    if d >= 1 and d <= rng then
                        local plotUnits = Map.GetUnitsAt(tx, ty)
                        if plotUnits then
                            for other in plotUnits:Units() do
                                local otherOwner = other:GetOwner()
                                if otherOwner ~= id and (otherOwner == 63 or Players[id]:GetDiplomacy():IsAtWarWith(otherOwner)) then
                                    -- LOS check for ranged units (d>1): verify the
                                    -- game engine agrees we can actually fire there.
                                    -- Melee (d==1) doesn't need LOS.
                                    local losOK = true
                                    if rs > 0 and d > 1 then
                                        -- 2026-08-06:这里原本是 {{}}。本函数返回的是
                                        -- 普通字符串(不是 f-string),花括号不会折叠,
                                        -- 下发到 Lua 就是 { [1] = {} } —— 一个数组位塞了
                                        -- 空表的表,而不是空参数表。参数编组被污染后
                                        -- CanStartOperation 对合法目标返回 false,
                                        -- get_units 的 targets 因此漏掉本可攻击的敌人
                                        -- (实机表现:明明能打却"看起来无法攻击")。
                                        -- 同函数的兄弟参数表(bParams / rp)一直是对的。
                                        local lp = {}
                                        lp[UnitOperationTypes.PARAM_X] = tx
                                        lp[UnitOperationTypes.PARAM_Y] = ty
                                        losOK = UnitManager.CanStartOperation(u, UnitOperationTypes.RANGE_ATTACK, nil, lp)
                                    end
                                    -- 2026-08-07:近战单位的跨域检查。此前这里
                                    -- 【只有远程走 LOS 校验,近战一格内一律放行】,
                                    -- 于是陆地近战单位会把海上单位列进 CAN ATTACK,
                                    -- 打过去静默无效(实机 T114/T115 连骗两回合)。
                                    -- 列出打不了的目标不只是浪费一次调用 ——
                                    -- 它会让人以为有一次免费攻击可用,从而改变整个
                                    -- 回合的部署(与 cities.py 城市射程那条同理)。
                                    if losOK and rs == 0 then
                                        losOK = meleeDomainOK(entry, tx, ty)
                                    end
                                    if losOK then
                                        local eInfo = GameInfo.Units[other:GetType()]
                                        local eName = eInfo and eInfo.UnitType or "UNKNOWN"
                                        local eHP = other:GetMaxDamage() - other:GetDamage()
                                        table.insert(tgtList, eName .. "@" .. tx .. "," .. ty .. "(" .. eHP .. "hp)")
                                    end
                                end
                            end
                        end
                    end
                end
            end
            if #tgtList > 0 then targets = table.concat(tgtList, ";") end
        end
        -- NOTE: promotion detection is intentionally omitted here.
        -- GetExperiencePoints() >= GetExperienceForNextLevel() stays true after
        -- SetPromotion() (level and XP are not consumed), so any mid-turn check
        -- fires one turn early AND causes double-promotions when end_turn's
        -- authoritative GameCore CanPromote check also fires.
        -- All promotion handling is routed through the end_turn blocker (which
        -- uses CanPromote in GameCore — the only correct check).
        local promo = "0"
        -- Upgrade info (InGame only: CanStartCommand)
        local canUp, upName, upCost = "0", "", "0"
        local ok1, _ = pcall(function()
            if UnitManager.CanStartCommand(u, UnitCommandTypes.UPGRADE, nil, true) then
                canUp = "1"
                local c2 = u:GetUpgradeCost()
                if c2 then upCost = tostring(c2) end
                if entry and entry.UpgradeUnitCollection then
                    for _, row in ipairs(entry.UpgradeUnitCollection) do
                        if row.UpgradeUnit then upName = row.UpgradeUnit end
                        break
                    end
                end
                -- 0c fix: 基础升级目标映射为本文明 UU(勇士→军团而非剑士)
                if upName ~= "" then upName = resolveUU(upName) end
            end
        end)
        -- Builder improvement advisor (InGame only: CanStartOperation)
        local validImps = ""
        if ut == "UNIT_BUILDER" and u:GetMovesRemaining() > 0 then
            local plot = Map.GetPlot(x, y)
            if plot and plot:GetOwner() == id then
                local impList = {}
                for imp in GameInfo.Improvements() do
                    if imp.Buildable and not imp.TraitType then
                        local bParams = {}
                        bParams[UnitOperationTypes.PARAM_X] = x
                        bParams[UnitOperationTypes.PARAM_Y] = y
                        bParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
                        local ok2, _ = pcall(function()
                            if UnitManager.CanStartOperation(u, UnitOperationTypes.BUILD_IMPROVEMENT, nil, bParams) then
                                table.insert(impList, imp.ImprovementType)
                            end
                        end)
                    end
                end
                if #impList > 0 then validImps = table.concat(impList, ";") end
            end
        end
        -- Military Engineer advisor (BUILD_ROUTE + fort/airstrip)
        if ut == "UNIT_MILITARY_ENGINEER" and u:GetMovesRemaining() > 0 then
            local meList = {}
            pcall(function()
                local opRow = GameInfo.UnitOperations["UNITOPERATION_BUILD_ROUTE"]
                if opRow then
                    local rp = {}
                    rp[UnitOperationTypes.PARAM_X] = x
                    rp[UnitOperationTypes.PARAM_Y] = y
                    if UnitManager.CanStartOperation(u, opRow.Hash, nil, rp) then
                        table.insert(meList, "BUILD_ROUTE")
                    end
                end
            end)
            local plot = Map.GetPlot(x, y)
            if plot and plot:GetOwner() == id then
                for imp in GameInfo.Improvements() do
                    if imp.Buildable and not imp.TraitType then
                        pcall(function()
                            local bp = {}
                            bp[UnitOperationTypes.PARAM_X] = x
                            bp[UnitOperationTypes.PARAM_Y] = y
                            bp[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
                            if UnitManager.CanStartOperation(u, UnitOperationTypes.BUILD_IMPROVEMENT, nil, bp) then
                                table.insert(meList, imp.ImprovementType)
                            end
                        end)
                    end
                end
            end
            if #meList > 0 then validImps = table.concat(meList, ";") end
        end
        -- 「这个单位在等你下指令吗」的权威源(2026-08-07 实机确认)。
        --
        -- 回合完成度门禁一直用「移动力满 = 整回合未动」当判据,那段代码的
        -- 注释写着「UnitInfo 没有 activity/orders 字段 —— 数据上无法区分
        -- 遗忘的单位和刻意驻守的单位」。实测下来这个前提是错的:
        --   IsReadyToSelect() 就是游戏 UI「下一个待指令单位」按钮的判据。
        --   已 fortify / 已 skip / 自动化 / 商路运行中的单位一律 false,
        --   只有真正在等指令的才 true —— 实机一次读回 9 个单位,
        --   精确命中唯一那个闲置建造者。
        -- 与「回合没提交=游戏在等你做决定」那条同一形态:权威判断游戏自己
        -- 早就在算,只是没人消费,于是我们用近似量(移动力)重新猜了一遍。
        --
        -- 顺带记下两个走过的弯路,免得下次再试:
        --   · ActivityTypes.ACTIVITY_OPERATION 对建造者不成立 —— 实测正在
        --     "建矿"的建造者是 ACTIVITY_AWAKE。Civ6 的建造者改良是**瞬时**
        --     完成的(消耗一个充能立即落地),压根没有"多回合作业中"状态。
        --   · GetQueuedOperationsCount 这个方法不存在;HasPendingOperations
        --     对改良中的建造者返回 false。
        local needsOrders = "0"
        pcall(function()
            if u:IsReadyToSelect() then needsOrders = "1" end
        end)
        print(uid .. "|" .. (uid % 65536) .. "|" .. nm .. "|" .. ut .. "|" .. x .. "," .. y .. "|" .. u:GetMovesRemaining() .. "/" .. u:GetMaxMoves() .. "|" .. (u:GetMaxDamage() - u:GetDamage()) .. "/" .. u:GetMaxDamage() .. "|" .. cs .. "|" .. rs .. "|" .. charges .. "|" .. targets .. "|" .. promo .. "|" .. canUp .. "|" .. upName .. "|" .. upCost .. "|" .. validImps .. "|" .. relName .. "|" .. needsOrders)
    end
end
-- ===================================================================
-- 阻塞性外交/交易会话探测(2026-08-07,用户实机报告)
--
-- 现象:AI 弹出"领袖头像 + 交易内容"的窗口,工具识别不到,后续所有
-- 动作照常下发但静默无效,整局就卡在那里。
--
-- 根因不是没有检测能力(get_diplomacy_sessions / get_pending_deals 都在),
-- 而是**检测只挂在 end_turn 的超时分支上**:回合中途弹出来的窗口,
-- 到下一次 end_turn 超时之前没有任何东西会提到它。中间发出去的
-- unit_action / city_action 全部白扔,而且每一条都返回"成功"。
--
-- 挂在 get_units 上,是因为回合完成度门禁强制每回合必调它 ——
-- 于是探测必然发生,且零额外往返。
-- ===================================================================
local dipN, dipWho = 0, {}
pcall(function()
    for i = 0, 62 do
        if i ~= id and Players[i] and Players[i]:IsAlive() then
            local sid = DiplomacyManager.FindOpenSessionID(id, i)
            if sid and sid >= 0 then
                dipN = dipN + 1
                local cn = "player " .. i
                pcall(function()
                    local cfg = PlayerConfigurations[i]
                    cn = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or cn
                end)
                table.insert(dipWho, cn .. "#" .. i)
            end
        end
    end
end)
local dealN, dealWho = 0, {}
pcall(function()
    for i = 0, 62 do
        if i ~= id and Players[i] and Players[i]:IsAlive() then
            local hasD = false
            pcall(function() hasD = DealManager.HasPendingDeal(id, i) end)
            if hasD then
                dealN = dealN + 1
                local cn = "player " .. i
                pcall(function()
                    local cfg = PlayerConfigurations[i]
                    cn = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or cn
                end)
                table.insert(dealWho, cn .. "#" .. i)
            end
        end
    end
end)
if dipN > 0 or dealN > 0 then
    print("BLOCKER|" .. dipN .. "|" .. table.concat(dipWho, ",") .. "|" .. dealN .. "|" .. table.concat(dealWho, ","))
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)
    )


def build_move_unit(unit_index: int, target_x: int, target_y: int) -> str:
    return f"""
{_lua_get_unit(unit_index)}
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Unit has no movement points remaining this turn. Use skip or wait until next turn.")}
end
if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO, nil, true) then
    -- 2026-08-12 缺陷113 的另一半:空军不走 MOVE_TO,它只能在航空港/航母
    -- 之间 REBASE。旧文案一律回 "invalid state",让人以为单位坏了或没移动力,
    -- 而真相是这类单位【永远】不能用 move —— 换个位置、等下回合都没用。
    local uiMv = GameInfo.Units[unit:GetType()]
    if uiMv ~= nil and tostring(uiMv.Domain or "") == "DOMAIN_AIR" then
        {_bail_lua('"ERR:AIR_UNIT_CANNOT_MOVE|空军不能用 move —— 它只在航空港/航母之间转场,不在地图上逐格走。要打远处目标:①直接 attack,空军无视地形视线,射程内即可;②射程不够就用 unit_action(action=\\"rebase\\", target_x/y=目标航空港) 转场过去。"')}
    end
    {_bail("ERR:CANNOT_MOVE|Unit cannot move (invalid state)")}
end
-- Pre-check: stacking conflict at target tile.
-- 2026-08-03 实机修复:此前是二值判断(isCivilian == otherCivilian),把
-- 「非平民」全归成一类。但文明6 有【三类】编队:CIVILIAN / 军事(LAND_COMBAT
-- 等) / SUPPORT(破城槌·攻城塔·医疗兵),规则是【同类不能同格,异类可以】。
-- 二值判断把 SUPPORT 当成军事单位,于是破城槌永远贴不上近战单位 ——
-- 而那正是它唯一的用法,等于支援单位整类不可用。改为比较真实 FormationClass。
local unitInfo = GameInfo.Units[unit:GetType()]
local myClass = (unitInfo and unitInfo.FormationClass) or ""
local tgtUnits = Map.GetUnitsAt({target_x}, {target_y})
if tgtUnits then
    for other in tgtUnits:Units() do
        if other:GetOwner() == me then
            local otherInfo = GameInfo.Units[other:GetType()]
            local otherClass = (otherInfo and otherInfo.FormationClass) or ""
            if myClass ~= "" and myClass == otherClass then
                local otherName = otherInfo and otherInfo.UnitType or "unit"
                {_bail_lua(f'"ERR:STACKING_CONFLICT|Friendly " .. otherName .. " already on ({target_x},{target_y}). Cannot stack same formation class (" .. myClass .. ")."')}
            end
        end
    end
end
local fromX, fromY = unit:GetX(), unit:GetY()
local params = {{}}
params[UnitOperationTypes.PARAM_X] = {target_x}
params[UnitOperationTypes.PARAM_Y] = {target_y}
-- Add ATTACK modifier if hostile unit on target tile (needed for civilian capture)
local hasHostile = false
if tgtUnits then
    for other in tgtUnits:Units() do
        if other:GetOwner() ~= me then hasHostile = true end
    end
end
if hasHostile then
    params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
end
UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
local tag = hasHostile and "OK:CAPTURE_MOVE|" or "OK:MOVING_TO|"
print(tag .. {target_x} .. "," .. {target_y} .. "|from:" .. fromX .. "," .. fromY)
print("{SENTINEL}")
"""


def build_unit_position_query(
    unit_index: int,
    move_target_x: int | None = None,
    move_target_y: int | None = None,
) -> str:
    """GameCore: read a unit's current position.

    When *move_target_x/y* are provided, also diagnoses why a blocked move
    failed (water, mountain, foreign border) so the caller doesn't need a
    second round-trip.
    """
    diag_block = ""
    if move_target_x is not None and move_target_y is not None:
        diag_block = f"""
-- Diagnose blocked move target
pcall(function()
    local plot = Map.GetPlot({move_target_x}, {move_target_y})
    if not plot then print("DIAG|UNKNOWN|tile does not exist"); return end
    -- 2026-08-07:目标格【被敌方单位占据】要最先判。
    -- 实机 T142:开拓者去 (51,30) 一直 BLOCKED,返回标签是 CAPTURE_MOVE
    -- (= 目标格有敌方单位),但诊断从水/山/领土一路查到 ZOC,唯独没查
    -- "目标格上站着人"这个最直接的原因。军事单位撞上去是攻击,
    -- 平民撞上去是【根本进不去】—— 两者的处置完全不同。
    local occWhy = nil
    pcall(function()
        local pu = Map.GetUnitsAt({move_target_x}, {move_target_y})
        if not pu then return end
        for other in pu:Units() do
            if other:GetOwner() ~= me then
                local oi = GameInfo.Units[other:GetType()]
                local on = oi and oi.UnitType or "UNKNOWN"
                -- 2026-08-10 罗马局 T172/T185:占格的两次都是【中立城邦】
                -- (普雷斯拉夫、维尔纽斯),文案却一律写"敌方",于是我两次
                -- 下了 attack 指令,靠 NOT_AT_WAR 门禁才没打成外交事故。
                -- 归属与战争状态必须写在诊断里。
                local oOwner = other:GetOwner()
                local oName = "player " .. oOwner
                pcall(function()
                    local cfg = PlayerConfigurations[oOwner]
                    oName = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or oName
                end)
                local atW = (oOwner == 63)
                pcall(function() atW = atW or Players[me]:GetDiplomacy():IsAtWarWith(oOwner) end)
                local who = (atW and "敌方(交战中) " or "非交战方 ") .. oName .. " 的 "
                local myCS = 0
                pcall(function()
                    local mi = u and GameInfo.Units[u:GetType()]
                    myCS = mi and (tonumber(mi.Combat) or 0) or 0
                end)
                if not atW then
                    occWhy = "目标格被" .. who .. on .. " 占据 —— 你和它【没有交战】,"
                        .. "attack 会被拒(NOT_AT_WAR)。绕路,或先谈开放边境"
                elseif myCS > 0 then
                    occWhy = "目标格被" .. who .. on .. " 占据。军事单位要进这一格得用 attack(移动过去=攻击),move 不会自动开打"
                else
                    occWhy = "目标格被" .. who .. on .. " 占据。**平民单位永远进不去敌占格** —— 换目标格,或先派军事单位清掉它"
                end
                return
            end
        end
    end)
    if occWhy ~= nil then print("DIAG|OCCUPIED|" .. occWhy); return end
    if plot:IsWater() then
        local hasShip = false
        pcall(function()
            local tech = GameInfo.Technologies["TECH_SHIPBUILDING"]
            if tech then hasShip = Players[me]:GetTechs():HasTech(tech.Index) end
        end)
        -- 2026-08-10 罗马局 T146 实机:水面格属【耶路撒冷领海】(非宗主禁入),
        -- 旧文案只报 "can embark" 让人反复重试。水面也要查归属。
        local wOwner = plot:GetOwner()
        if wOwner >= 0 and wOwner ~= me then
            local wName = "player " .. wOwner
            pcall(function()
                local cfg = PlayerConfigurations[wOwner]
                wName = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or wName
            end)
            local wOB = false
            pcall(function() wOB = Players[me]:GetDiplomacy():HasOpenBordersFrom(wOwner) end)
            local wMajor = true
            pcall(function() wMajor = Players[wOwner]:IsMajor() end)
            if wOB then
                print("DIAG|WATER_OK|water tile in " .. wName .. " waters (Open Borders active, can embark)")
            elseif wMajor then
                print("DIAG|WATER_BORDER|" .. wName .. " territorial waters - need Open Borders via propose_trade")
            else
                print("DIAG|WATER_BORDER_CS|" .. wName .. " (city-state) territorial waters - closed to non-suzerains")
            end
        elseif hasShip then print("DIAG|WATER_OK|water tile (can embark)")
        else print("DIAG|WATER|water tile - land units need Shipbuilding tech to embark") end
    elseif plot:IsMountain() then
        print("DIAG|MOUNTAIN|impassable mountain")
    elseif plot:IsImpassable() then
        print("DIAG|IMPASSABLE|impassable terrain (ice or natural wonder)")
    else
        local owner = plot:GetOwner()
        if owner >= 0 and owner ~= me then
            local atWar = false
            pcall(function() atWar = Players[me]:GetDiplomacy():IsAtWarWith(owner) end)
            if atWar then
                print("DIAG|UNKNOWN|tile is enemy territory but movement still blocked - check path")
            else
                local civName = "player " .. owner
                pcall(function()
                    local cfg = PlayerConfigurations[owner]
                    civName = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or civName
                end)
                local isMajor = true
                pcall(function() isMajor = Players[owner]:IsMajor() end)
                -- 2026-08-10 罗马局 T136 实机:已买到开放边界后这里仍甩锅
                -- "need Open Borders",真实障碍另有其因(格上有单位/地形)。
                -- 先查 HasOpenBordersFrom,已开放就别误导。
                local hasOB = false
                pcall(function() hasOB = Players[me]:GetDiplomacy():HasOpenBordersFrom(owner) end)
                if hasOB then
                    print("DIAG|BORDER_OPEN|" .. civName .. " territory but Open Borders IS active - blocker is something else on the path (unit/terrain); try single-step moves")
                elseif isMajor then
                    print("DIAG|BORDER|foreign territory (" .. civName .. ") - need Open Borders via propose_trade")
                else
                    print("DIAG|BORDER_CS|city-state territory (" .. civName .. ") - need suzerainty (their land AND water are closed to non-suzerains)")
                end
            end
        else
            -- 2026-08-07 实机(苏美尔局 T131/T142,累计浪费 6+ 次调用):
            -- 到这里说明【目标格本身没问题】,障碍在路上。此前就到此为止,
            -- 只印一句 "path may be blocked by intermediate tiles" ——
            -- 一句无信息的话:它既没说是哪一格,也没说为什么。
            -- 我照着"路径问题"连试了 4 个方向都 BLOCKED,最后靠"宣战后
            -- 同一格立刻能走"才反推出真因是【未开放边境】。
            --
            -- 修法:沿 GetMoveToPath 逐格找出第一个走不通的格子,对那一格
            -- 复用上面同一套判据。诊断的对象必须是【真正挡路的那一格】,
            -- 不是你想去的那一格。
            -- ⚠️ 本查询里单位变量叫 u(不是 unit)。写错名字不会报错,
            -- 只会让整个 pcall 静默失败,诊断永远落到最后那句 UNKNOWN ——
            -- 正是本次要修掉的那种"看起来在工作、其实什么都没做"。
            local named = false
            pcall(function()
                if u == nil then return end
                if u:GetMovesRemaining() <= 0 then
                    print("DIAG|NO_MOVES|本回合移动力已耗尽(" .. string.format("%.1f", u:GetMovesRemaining()) .. "/" .. string.format("%.1f", u:GetMaxMoves()) .. ")—— 不是路径问题,下回合再走")
                    named = true
                    return
                end
                local path = UnitManager.GetMoveToPath(u, plot:GetIndex())
                if not path or #path == 0 then
                    print("DIAG|NO_PATH|引擎算不出到 (" .. {move_target_x} .. "," .. {move_target_y} .. ") 的任何路径 —— 目标可能在另一块大陆,或被地形/领土完全隔断。先用 get_pathing_estimate 确认可达性,不要换方向反复试")
                    named = true
                    return
                end
                for i = 2, #path do
                    local p = Map.GetPlotByIndex(path[i])
                    if p then
                        local px, py = p:GetX(), p:GetY()
                        local why = nil
                        if p:IsWater() then why = "水面(需造船术才能渡海)"
                        elseif p:IsMountain() then why = "山脉(不可通行)"
                        elseif p:IsImpassable() then why = "不可通行地形(冰川/自然奇观)"
                        else
                            local o = p:GetOwner()
                            if o >= 0 and o ~= me then
                                local atW = false
                                pcall(function() atW = Players[me]:GetDiplomacy():IsAtWarWith(o) end)
                                if not atW then
                                    local cn = "player " .. o
                                    pcall(function()
                                        local cfg = PlayerConfigurations[o]
                                        cn = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or cn
                                    end)
                                    why = "他国领土(" .. cn .. ")且未开放边境 —— 换方向没用,要么谈开放边境(propose_trade),要么绕路"
                                end
                            end
                        end
                        if why ~= nil then
                            print("DIAG|PATH_BLOCKED|真正挡路的是 (" .. px .. "," .. py .. "):" .. why .. "。目标格 (" .. {move_target_x} .. "," .. {move_target_y} .. ") 本身没问题")
                            named = true
                            return
                        end
                    end
                end
            end)
            if not named then
                print("DIAG|UNKNOWN|目标格可通行,逐格扫描也没找到静态障碍 —— 多半是敌方单位的控制区(ZOC)或己方单位堵路。用 get_units 看看路上有没有单位")
            end
        end
    end
end)
"""
    return f"""
local me = Game.GetLocalPlayer()
local u = Players[me]:GetUnits():FindID({unit_index})
if u then print("POS|" .. u:GetX() .. "|" .. u:GetY()) else print("POS|GONE") end
-- 2026-08-10 罗马局 T174-190(6 次实锤):陆战单位 move 到【陆地】目标,
-- 引擎寻路从水面抄近道 —— 单位自动登船、移动力耗尽、当回合搁浅海上。
-- 其中一次是给 13 血火枪手下的【撤退令】,结果把它送进了敌城射程。
-- 登船单位战力归零、一击即死,所以位置重读这一帧必须把它喊出来
-- (同帧读不到:移动是异步的,所以判定放在这次延迟重读里)。
if u then
    pcall(function()
        local emb = false
        pcall(function() emb = u:IsEmbarked() end)
        if not emb then
            local p = Map.GetPlot(u:GetX(), u:GetY())
            if p and p:IsWater() then emb = true end
        end
        if emb then print("EMBARKED|1") end
    end)
end
{diag_block}print("{SENTINEL}")
"""


def build_attack_unit(unit_index: int, target_x: int, target_y: int) -> str:
    return f"""
{_LUA_HEX_DIST}
{_LUA_MELEE_DOMAIN}
{_lua_get_unit(unit_index)}
local ux, uy = unit:GetX(), unit:GetY()
-- 2026-08-06:距离改用 hexDist。Map.GetPlotDistance 在 offset 网格上有
-- 实测误报(_helpers.py 的铁律、方案 v5 铁律五),而这个 dist 下面要拿去
-- 做 OUT_OF_RANGE 硬拦截 —— 误报一格就会把合法的远程攻击挡掉。
-- 近战路径早就不拿它当门禁了(见下方注释),远程/空军一直漏网。
local dist = hexDist(ux, uy, {target_x}, {target_y})
-- Find hostile unit on target tile (military first; civilians only after the
-- city check — see 缺陷117 below)
local enemy = nil
local enemyName = "unknown"
local civilianFallback = nil
local civilianFallbackName = "unknown"
local tgtUnits = Map.GetUnitsAt({target_x}, {target_y})
if tgtUnits then
    local fallback = nil
    local fallbackName = "unknown"
    for other in tgtUnits:Units() do
        if other:GetOwner() ~= me then
            local eInfo = GameInfo.Units[other:GetType()]
            local eName = eInfo and eInfo.UnitType or "UNKNOWN"
            local eCombat = eInfo and eInfo.Combat or 0
            if eCombat > 0 then
                enemy = other
                enemyName = eName
                break
            elseif fallback == nil then
                fallback = other
                fallbackName = eName
            end
        end
    end
    -- 2026-08-12 缺陷117:平民 fallback 原本在这里【无条件】接管,排在城市
    -- 检查之前。于是攻击一座城时,只要 Map.GetUnitsAt 在城格上返回任何一个
    -- 平民,攻击就全部导向那个平民而不是城市。
    --
    -- T455-459 实测旧金山(17,46):连打六发,每发都报 target:UNIT_TRADER、
    -- damage dealt:100,而城墙 100/100、守军 200/200 六回合纹丝不动,那个
    -- 商人也一直满血。同一格用 Units.GetUnitsInPlot 查是【0 个单位】——
    -- Map.GetUnitsAt 返回的是贸易路线上路过的商人,它不驻扎、也打不到。
    --
    -- 规则上也该这么改:城市格里只有【驻军】参与防御,平民不参战。所以
    -- 平民 fallback 必须让位给城市 —— 见下面 cityTarget 之后的收尾。
    civilianFallback = fallback
    civilianFallbackName = fallbackName
end
-- 2026-08-02 校准局实战 P0:此前只找目标格上的敌方【单位】,无驻军的
-- 敌城直接 NO_ENEMY —— 城市根本打不了,统治路线在工具层走不通。
-- 目标格无单位时检查是否敌城:是则按城市攻击执行(近战=MOVE_TO+ATTACK,
-- 远程=RANGE_ATTACK 对城,引擎自行结算城防)。
local cityTarget = nil
if enemy == nil then
    -- 2026-08-10 罗马局 T187:此前只用 Cities.GetCityInPlot,拿到的对象上
    -- GetDistricts/GetDamage 读不出真实城血(战报一路报 "city HP:200 -> 200/200",
    -- 而 lua 直查是 183)—— 于是"打了没效果"和"打了有效果"在战报里长得一样,
    -- 围城打到第 4 次才发现不对。InGame 下 CityManager.GetCityAt 才是可靠句柄。
    local pC = nil
    pcall(function() pC = CityManager.GetCityAt({target_x}, {target_y}) end)
    if pC == nil then
        pcall(function() pC = Cities.GetCityInPlot({target_x}, {target_y}) end)
    end
    if pC ~= nil and pC:GetOwner() ~= me then
        cityTarget = pC
        enemyName = "CITY " .. Locale.Lookup(pC:GetName())
    end
end
-- 缺陷117 的收尾:平民只有在【目标格不是敌城】时才成为目标。城市格上的
-- 平民打不到(受城防保护),把攻击导给它等于六发全打空。
if enemy == nil and cityTarget == nil and civilianFallback ~= nil then
    enemy = civilianFallback
    enemyName = civilianFallbackName
end
if enemy == nil and cityTarget == nil then
    {_bail(f"ERR:NO_ENEMY|No hostile unit or city at ({target_x},{target_y})")}
end
-- Check diplomatic status — can only attack targets you're at war with (barbarians always attackable)
local enemyOwner = enemy and enemy:GetOwner() or cityTarget:GetOwner()
if enemyOwner ~= 63 then
    local pDiplo = Players[me]:GetDiplomacy()
    if not pDiplo:IsAtWarWith(enemyOwner) then
        local ownerCfg = PlayerConfigurations[enemyOwner]
        local ownerName = ownerCfg and Locale.Lookup(ownerCfg:GetCivilizationDescription()) or ("player " .. enemyOwner)
        {_bail_lua('"ERR:NOT_AT_WAR|Cannot attack " .. enemyName .. " — you are at peace with " .. ownerName .. ". Declare war first or target a different unit."')}
    end
end
local enemyHP, enemyMaxHP = 0, 0
if enemy then
    enemyHP = enemy:GetMaxDamage() - enemy:GetDamage()
    enemyMaxHP = enemy:GetMaxDamage()
else
    -- 2026-08-08 T194 缺陷(34):City 对象上 GetMaxDamage/GetDamage 不存在,
    -- pcall 吞掉后读成 0/0,触发假 CITY_WILL_RAZE("远程攻击 0 血城市将夷平"),
    -- 查尔斯顿墙 273/300 时连报三次。正确读法是遍历 districts 找 CITY_CENTER
    -- 在 district 对象上调(cities.py:53 同款;同文件 :768 战报路径早就这么写,
    -- 又是一个概念两套实现)。
    pcall(function()
        local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
        for _, d in cityTarget:GetDistricts():Members() do
            if d:GetType() == ccIdx then
                enemyMaxHP = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                enemyHP = enemyMaxHP - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                break
            end
        end
    end)
end
local myHP = unit:GetMaxDamage() - unit:GetDamage()
local params = {{}}
params[UnitOperationTypes.PARAM_X] = {target_x}
params[UnitOperationTypes.PARAM_Y] = {target_y}
-- Determine attack type
local unitInfo = GameInfo.Units[unit:GetType()]
-- 2026-08-04 P0 修复:远程类别由数据表判定,不再用 CanStartOperation
-- (参数位错误导致对合法目标返回 false,见下)。Civ6 远程单位
-- (RangedCombat>0 或 Bombard>0)没有近战攻击操作,任何情况下都不允许
-- 降级 melee —— 降级只会打印假成功(T124/T158 两次被骗,弓箭手打海上
-- GALLEY 报 OK:MELEE_ATTACK 但 HP 纹丝不动)。字段名以仓库实测为准:
-- units.py 估算函数用 RangedCombat;Catapult/Trebuchet 远程靠 Bombard。
-- 2026-08-12 缺陷113:isAir 原本写作 `(not isRanged) and CanStartOperation(...)`。
-- 空军【当然】有远程攻击力(喷气式轰炸机 RangedCombat=120),于是 isRanged 恒为
-- 真、isAir 恒为假,下面那段 AIR_ATTACK 分支从来没有被执行过 —— 空军一律走地面
-- 远程路径,被 LOS 检查毙掉,报 "NO_LOS: Move to a tile with line of sight"。
-- 而空军根本不该受地形视线约束,也不能用 move 挪位置(它只能 REBASE),两条路
-- 同时堵死。实机 T367:花 2715 金买的喷气式轰炸机一发打不出去,用户只好离开
-- MCP 去游戏界面手动点 —— 工具把人赶回了 UI,这是最彻底的失效。
--
-- 判据换成先问空军、再问远程。Domain 是权威(DOMAIN_AIR),CanStartOperation
-- 作为兜底 —— 实测轰炸机对 AIR_ATTACK 返回 true,而地面远程单位返回 false,
-- 所以两者都不会误判。
local isAir = false
if unitInfo ~= nil then
    isAir = (tostring(unitInfo.Domain or "") == "DOMAIN_AIR")
end
if not isAir then
    local okAir, canAir = pcall(function()
        return UnitManager.CanStartOperation(unit, UnitOperationTypes.AIR_ATTACK, nil, params)
    end)
    isAir = (okAir and canAir) == true
end
local isRanged = (not isAir) and unitInfo ~= nil
    and ((tonumber(unitInfo.RangedCombat) or 0) > 0
         or (tonumber(unitInfo.Bombard) or 0) > 0)
if isRanged then
    if unit:GetMovesRemaining() <= 0 then
        {_bail("ERR:NO_MOVES|Unit has no movement points for ranged attack. Ranged attacks require movement. Move and attack on separate turns, or attack before moving.")}
    end
    local rng = unitInfo and unitInfo.Range or 1
    if dist > rng then
        {_bail_lua('"ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but range is " .. rng .. ". Move closer first."')}
    end
    -- LOS check: CanStartOperation with target params is authoritative;
    -- GetOperationTargets returns empty for some valid targets (naval units, etc.)
    -- 2026-08-04 实机修复(校准局 P0):远程攻击 0 血城市 = 夷平而非占领
    -- (实机:弩手打掉 0 血亚丁,城市直接 eliminated 消失,本可占领)。
    -- 执行前显式警告,让 Agent 改用近战收尾。
    if cityTarget ~= nil and enemyHP <= 0 then
        print("WARN:CITY_WILL_RAZE|远程攻击 0 血城市将夷平它(城市消失,不归你)。想占领请改用近战单位(近战攻击或移动到城格)收尾。")
    end
    local losParams = {{}}
    losParams[UnitOperationTypes.PARAM_X] = {target_x}
    losParams[UnitOperationTypes.PARAM_Y] = {target_y}
    local canRanged = UnitManager.CanStartOperation(unit, UnitOperationTypes.RANGE_ATTACK, nil, losParams)
    if canRanged then
        UnitManager.RequestOperation(unit, UnitOperationTypes.RANGE_ATTACK, params)
        print("OK:RANGE_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})|pre_hp:" .. enemyHP .. "/" .. enemyMaxHP .. "|your HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
        print("{SENTINEL}"); return
    else
        -- 2026-08-04 P0 修复:远程单位攻击失败【绝不降级近战】。
        -- 实机:弓箭手打 1 格外海上 GALLEY,RANGE_ATTACK 失败(LOS 被悬崖挡)
        -- 后降级 melee,RequestOperation 静默失败却打印 OK:MELEE_ATTACK
        -- (两次"成功"但 HP 纹丝不动、移动力未消耗)。远程单位没有近战
        -- 操作,降级永远是错 —— 返回真实原因,让调用方换位置/换单位。
        -- 2026-08-06:此前无论什么原因失败,一律报 "LOS blocked" ——
        -- Agent 收到"视线被挡"就去换位置,而真实原因(目标在迷雾里、
        -- 本回合已攻击)换位置根本解决不了,于是反复白试(实测 3 次)。
        -- 先把能确定的原因分开报,都不命中才归到视线。
        local vis = true
        local tPlot2 = Map.GetPlot({target_x}, {target_y})
        pcall(function() vis = tPlot2 and tPlot2:IsVisible(me) end)
        if not vis then
            {_bail_lua(f'"ERR:NOT_VISIBLE|({target_x},{target_y}) 当前不在你的视野内(迷雾),无法攻击。换位置不解决 —— 先派单位/侦察兵把那一格照亮,或攻击你看得见的目标。"')}
        end
        local mv = 0
        pcall(function() mv = unit:GetMovesRemaining() end)
        if mv <= 0 then
            {_bail_lua('"ERR:ALREADY_ATTACKED|该单位本回合已耗尽移动力(远程攻击会耗尽全部移动力),不能再攻击。远程的正确顺序是【先攻击、再考虑移动】;下回合开始时先打再走。"')}
        end
        local why = "LOS blocked (cliff/terrain)"
        local tPlot = Map.GetPlot({target_x}, {target_y})
        if tPlot and tPlot:IsWater() then
            why = "water target not reachable by land units (cliff LOS block)"
        end
        {_bail_lua(f'"ERR:NO_LOS|Ranged unit cannot melee-attack. RANGE_ATTACK failed at ({target_x},{target_y}) from (" .. ux .. "," .. uy .. "): " .. why .. ". Move to a tile with line of sight or use a naval unit."')}
    end
end
if isAir then
    -- Air units (jet bombers, jet fighters, bombers, fighters): use AIR_ATTACK operation.
    -- Combat resolves asynchronously in the UI so post-combat HP reads may be stale.
    local rng = unitInfo and unitInfo.Range or 1
    if dist > rng then
        {_bail_lua('"ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but air range is " .. rng .. ". Rebase closer first."')}
    end
    -- 2026-08-12 缺陷118:这里原本【不预检】就 RequestOperation 然后打印成功。
    -- T455-459 实测旧金山:连续六发都报 "damage dealt:100",而城墙 100/100
    -- 六回合一动不动 —— 引擎其实一次都没受理。查 CanStartOperation 返回 false,
    -- 真因是那架轰炸机停在 (26,46),而这一格【不是航空港】:空军只能从
    -- 航空港/航母出击,停在野外就打不出去。射程够、有移动力、目标可见,
    -- 三个看得见的条件全满足,唯一不满足的那个恰恰没人检查。
    --
    -- 假成功比失败贵得多:失败会让人换方案,假成功让人以为在推进 ——
    -- 六个回合的空军火力就这么没了,而战报每一行都写着命中。
    local okAir, canAir = pcall(function()
        return UnitManager.CanStartOperation(unit, UnitOperationTypes.AIR_ATTACK, nil, params, true)
    end)
    if not (okAir and canAir) then
        local atPort = "否"
        pcall(function()
            local myPlot = Map.GetPlot(ux, uy)
            local dt = myPlot and myPlot:GetDistrictType() or -1
            if dt and dt >= 0 then
                local dr = GameInfo.Districts[dt]
                if dr and (dr.DistrictType == "DISTRICT_AERODROME" or dr.Airfield) then atPort = "是" end
            end
        end)
        {_bail_lua('"ERR:AIR_ATTACK_REFUSED|引擎拒绝了这次空袭(射程 " .. rng .. " 够、距离 " .. dist .. "、移动力 " .. unit:GetMovesRemaining() .. ")。当前位置 (" .. ux .. "," .. uy .. ") 是航空港/航母吗:" .. atPort .. "。空军【只能从航空港或航母出击】,停在野外或已被占领的机场都打不出去 —— 先 unit_action(action=\\"rebase\\") 转场到己方航空港再打。其它可能:本回合已出击、目标被防空覆盖。"')}
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.AIR_ATTACK, params)
    print("OK:AIR_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})|pre_hp:" .. enemyHP .. "/" .. enemyMaxHP .. "|bomber HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
else
    -- Melee: let CanStartOperation be the authority on adjacency/validity.
    -- Map.GetPlotDistance can misreport distance on offset hex grids, so we
    -- do not use it as a gate here — only as a diagnostic in the error message.
    local myCS = unitInfo and unitInfo.Combat or 0
    -- Movement check: melee attack requires movement points (ranged does not)
    if unit:GetMovesRemaining() <= 0 then
        {_bail("ERR:NO_MOVES|Unit has no movement points for melee attack. Melee requires movement to close distance. Wait until next turn.")}
    end
    -- ZOC check: if unit entered enemy ZOC this turn, it cannot attack until next turn.
    -- CanStartOperation returns true but RequestOperation silently queues for next turn.
    if unit:HasMovedIntoZOC() then
        {_bail_lua('"ERR:ZOC|Unit entered Zone of Control this turn — cannot attack until next turn. End turn and attack from current position next turn."')}
    end
    -- 2026-08-07 P0:跨域近战拦截。引擎对「陆战单位打水面目标」返回
    -- CanStartOperation=true 但 RequestOperation 静默不执行,于是下面
    -- 一路走到 print("OK:MELEE_ATTACK") —— 假成功。必须在请求前挡掉。
    local domOK, domWhy = meleeDomainOK(unitInfo, {target_x}, {target_y})
    if not domOK then
        if domWhy == "LAND_VS_WATER" then
            {_bail_lua(f'"ERR:DOMAIN_MISMATCH|" .. enemyName .. " 在水面格 ({target_x},{target_y}),陆战近战单位打不到它 —— 近战攻击=移动到该格,陆地单位进不了水。引擎不会报错,只会静默不执行(表现为「攻击成功但 HP 不变」)。改用远程单位(弓箭手/弩手/投石车)或海军单位,或者等它靠岸/进港。"')}
        else
            {_bail_lua(f'"ERR:DOMAIN_MISMATCH|" .. enemyName .. " 在陆地格 ({target_x},{target_y}),海军近战单位只能攻击水面单位或沿海城市。改用陆战/远程单位。"')}
        end
    end
    params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
    if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO, nil, params) then
        {_bail_lua(f'"ERR:ATTACK_BLOCKED|Cannot attack " .. enemyName .. " at ({target_x},{target_y}) (map dist=" .. dist .. "). Unit not adjacent or blocked by popup/diplomacy."')}
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
    -- Verify unit reached adjacency (MOVE_TO resolves synchronously for movement)
    local newX, newY = unit:GetX(), unit:GetY()
    local newDist = hexDist(newX, newY, {target_x}, {target_y})
    if newDist > 1 then
        -- 2026-08-09 (35):MOVE_TO+ATTACK 是异步结算,同帧读到的位置可能是
        -- 中途快照 —— T204 战车打长枪、T205 战车攻城均报 stopped short,
        -- 前者实已命中、后者单位当场阵亡。这里只能给不确定陈述,
        -- 服务端(game_state)会延迟重读单位存活与目标格状态改写结论。
        print("ERR:STOPPED_SHORT|Unit at (" .. newX .. "," .. newY .. ") reads " .. newDist .. " tiles from ({target_x},{target_y}) in the same frame — movement+attack resolve ASYNC, so the attack may still have resolved. Do NOT re-attack before re-reading unit and target state.")
        print("{SENTINEL}"); return
    end
    -- Try to read post-combat state (may fail if units moved/died)
    local myAfterHP = myHP
    local ok1, _ = pcall(function() myAfterHP = unit:GetMaxDamage() - unit:GetDamage() end)
    local report = "OK:MELEE_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})"
    if cityTarget ~= nil then
        -- 城市目标:重读城血;单位已进城格 = 占领
        -- 2026-08-08 (34):City 对象无此 API,走 district(同上)
        local cAfter = enemyHP
        pcall(function()
            local ccIdx2 = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
            for _, d in cityTarget:GetDistricts():Members() do
                if d:GetType() == ccIdx2 then
                    cAfter = (d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0) - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                    break
                end
            end
        end)
        report = report .. "|city HP:" .. enemyHP .. " -> " .. cAfter .. "/" .. enemyMaxHP
        local nx, ny = unit:GetX(), unit:GetY()
        if nx == {target_x} and ny == {target_y} then
            report = report .. "|CITY_CAPTURED"
        elseif cAfter >= enemyHP then
            -- 2026-08-10 伦敦:连续四次近战城血几乎不动,战报却每次都像成功。
            -- 城血没掉 = 这一击白打(城防远高于你的战力,且城每回合回 ~20)。
            report = report .. "|NO_PROGRESS(城血未下降 —— 你的战力打不穿这座城的城防,"
                .. "换攻城单位/升级兵种,别再拿人填)"
        end
    else
        local enemyAfterHP = 0
        local enemyAlive = false
        local ok2, _ = pcall(function()
            local d = enemy:GetDamage()
            if d ~= nil then enemyAfterHP = enemy:GetMaxDamage() - d; enemyAlive = true end
        end)
        if enemyAlive then
            report = report .. "|enemy HP:" .. enemyHP .. " -> " .. enemyAfterHP .. "/" .. enemyMaxHP
        else
            report = report .. "|enemy HP:" .. enemyHP .. " -> KILLED"
        end
    end
    report = report .. "|your HP:" .. myHP .. " -> " .. myAfterHP .. " CS:" .. myCS
    print(report)
end
print("{SENTINEL}")
"""


def build_unit_hp_query(unit_index: int) -> str:
    """InGame context: read a single unit's current HP (post-combat truth).

    2026-08-02 校准局 0d:攻击返回里的 ``your HP:A -> B`` 两侧都是战前值
    (同帧读不到战后状态)。战损必须在延迟后的独立帧里重读 —— 本查询
    就是那次重读。单位已阵亡时返回 ERR:UNIT_NOT_FOUND/UNIT_STALE。
    """
    return f"""
{_lua_get_unit(unit_index)}
print("OK:UNIT_HP|" .. (unit:GetMaxDamage() - unit:GetDamage()) .. "/" .. unit:GetMaxDamage())
-- 位置同样是延迟帧才可靠:MOVE_TO 的结算不在请求那一帧完成,
-- 同帧读到的是中途快照(2026-08-03 实机:报 33,35 实际 32,36)。
print("OK:UNIT_POS|" .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_attack_followup_query(target_x: int, target_y: int) -> str:
    """InGame context: get actual HP of units at target tile after combat.

    Also checks for city defenses (walls/garrison) at the target — when
    attacking a walled city, damage goes to walls first so the garrison
    unit's HP stays unchanged even though the attack succeeded.

    Runs in InGame context because enemy city district APIs
    (GetDistricts, GetMaxDamage) are not available in GameCore.
    """
    return f"""
local found = false
for i = 0, 63 do
    if Players[i] and Players[i]:IsAlive() then
        for _, u in Players[i]:GetUnits():Members() do
            if u:GetX() == {target_x} and u:GetY() == {target_y} then
                local hp = u:GetMaxDamage() - u:GetDamage()
                local entry = GameInfo.Units[u:GetType()]
                local name = entry and entry.UnitType or "UNKNOWN"
                print("UNIT|" .. name .. "|" .. hp .. "/" .. u:GetMaxDamage() .. "|owner:" .. i)
                found = true
            end
        end
        pcall(function()
            for _, c in Players[i]:GetCities():Members() do
                if c:GetX() == {target_x} and c:GetY() == {target_y} then
                    local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
                    for _, d in c:GetDistricts():Members() do
                        if d:GetType() == ccIdx then
                            pcall(function()
                                local wMax = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                local wHP = wMax - (d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0)
                                local gMax = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                                local gHP = gMax - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                                if wMax > 0 or gMax > 0 then
                                    print("CITY_DEF|wall:" .. wHP .. "/" .. wMax .. "|garrison:" .. gHP .. "/" .. gMax)
                                end
                            end)
                            break
                        end
                    end
                end
            end
        end)
    end
end
if not found then print("EMPTY") end
print("{SENTINEL}")
"""


def parse_blocked_diagnostic(lines: list[str]) -> str:
    """Extract human-readable block reason from diagnostic Lua output."""
    for line in lines:
        if line.startswith("DIAG|"):
            parts = line.split("|", 2)
            if len(parts) >= 3:
                return parts[2]
    return "unit did not move — impassable terrain, border, or no path"


def build_combat_estimate_query(unit_index: int, target_x: int, target_y: int) -> str:
    """InGame context: gather combat stats for damage estimation (no attack executed).

    Includes: base CS, promotions, fortification, terrain (hills, forest/jungle),
    river crossing, flanking bonus, and support bonus.
    """
    return f"""
{_lua_get_unit(unit_index)}
local ux, uy = unit:GetX(), unit:GetY()
local dist = Map.GetPlotDistance(ux, uy, {target_x}, {target_y})
local unitInfo = GameInfo.Units[unit:GetType()]
local attType = unitInfo and unitInfo.UnitType or "UNKNOWN"
local attCS = unitInfo and unitInfo.Combat or 0
-- 攻城类走 Bombard 列,普通远程走 RangedCombat 列 —— 取两者较大者才覆盖全部。
-- 2026-08-11 T291 实锤:大炮(Combat=60/RangedCombat=0/Bombard=80)被判成近战,
-- 估算报"造成 ~16 却要承受 ~35"并被亏本交易门拦下。实际它是射程 2 的
-- 攻城单位,零反伤且专克城墙 —— 统治路线的核心兵种就这样被废掉了。
-- 执行侧(build_attack_unit 的远程门)早就同时看 RangedCombat 和 Bombard,
-- 估算侧漏了,于是"能打"和"值不值得打"两个判断用了不同的兵种模型。
local attRS = math.max(
    tonumber(unitInfo and unitInfo.RangedCombat) or 0,
    tonumber(unitInfo and unitInfo.Bombard) or 0)
-- 2026-08-02 校准局 P0 #6:此前是 attRS > 0 and dist > 1 ——
-- 射程 1 的投石兵贴脸(dist==1)攻击被判成近战估算(CS 5 对 20,
-- 报"我方承受 ~65"),而实际执行走 RANGE_ATTACK 零反伤。
-- 远程单位攻击【永远】用远程结算,与距离无关(执行侧仅在 canRanged
-- 失败时才回落近战,属异常路径)。
local isRanged = attRS > 0
local effAttCS = isRanged and attRS or attCS
-- Find defender
local enemy = nil
local tgtUnits = Map.GetUnitsAt({target_x}, {target_y})
if tgtUnits then
    for other in tgtUnits:Units() do
        if other:GetOwner() ~= me then
            local eInfo = GameInfo.Units[other:GetType()]
            local eCombat = eInfo and eInfo.Combat or 0
            if eCombat > 0 or enemy == nil then enemy = other end
            if eCombat > 0 then break end
        end
    end
end
-- 2026-08-10 罗马局 T177-191(伦敦):目标是【城市】时,此前只找守军单位,
-- 于是拿弓箭手 CS15 当防御方,估算报"~1906 伤 → LIKELY KILL",而真正的
-- 防御方是城中心(城防 ~80)。城市目标必须用城防值估算,否则每一次攻城
-- 决策都建立在假数据上。
local cityDef = nil
local cityName, cityDefStr, cityHP, cityMaxHP = "", 0, 0, 0
do
    local c = nil
    pcall(function() c = CityManager.GetCityAt({target_x}, {target_y}) end)
    if c ~= nil and c:GetOwner() ~= me then
        cityDef = c
        pcall(function() cityName = Locale.Lookup(c:GetName()) end)
        local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
        pcall(function()
            for _, d in c:GetDistricts():Members() do
                if d:GetType() == ccIdx then
                    pcall(function() cityDefStr = d:GetDefenseStrength() or 0 end)
                    cityMaxHP = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                    cityHP = cityMaxHP - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                    break
                end
            end
        end)
    end
end
if enemy == nil and cityDef == nil then {_bail(f"ERR:NO_ENEMY|No hostile unit or city at ({target_x},{target_y})")} end
if cityDef ~= nil then
    local cOwner = cityDef:GetOwner()
    local atWar = false
    pcall(function() atWar = Players[me]:GetDiplomacy():IsAtWarWith(cOwner) end)
    if cOwner ~= 63 and not atWar then
        local cfg = PlayerConfigurations[cOwner]
        local oName = cfg and Locale.Lookup(cfg:GetCivilizationDescription()) or ("player " .. cOwner)
        {_bail_lua('"ERR:NOT_AT_WAR|" .. cityName .. " belongs to " .. oName .. " — you are at peace. Declare war first."')}
    end
    -- 城市没有近战反击"单位",但攻城方会吃城防反击;估算按城防值走。
    local myHPc = unit:GetMaxDamage() - unit:GetDamage()
    print("ESTIMATE|" .. attType .. "|CITY " .. cityName .. "|" .. effAttCS .. "|" .. cityDefStr
        .. "|" .. (isRanged and "1" or "0") .. "|city defense (garrison unit ignored)|"
        .. myHPc .. "|" .. cityHP)
    print("{SENTINEL}")
    return
end
-- Check diplomatic status — estimates for units at peace are misleading
local enemyOwner = enemy:GetOwner()
if enemyOwner ~= 63 then
    local pDiplo = Players[me]:GetDiplomacy()
    if not pDiplo:IsAtWarWith(enemyOwner) then
        local ownerCfg = PlayerConfigurations[enemyOwner]
        local ownerName = ownerCfg and Locale.Lookup(ownerCfg:GetCivilizationDescription()) or ("player " .. enemyOwner)
        local eInfo2 = GameInfo.Units[enemy:GetType()]
        local eName = eInfo2 and eInfo2.UnitType or "UNKNOWN"
        {_bail_lua('"ERR:NOT_AT_WAR|Cannot attack " .. eName .. " — you are at peace with " .. ownerName .. ". Declare war first."')}
    end
end
local eInfo = GameInfo.Units[enemy:GetType()]
local defType = eInfo and eInfo.UnitType or "UNKNOWN"
local defCS = eInfo and eInfo.Combat or 0
local enemyHP = enemy:GetMaxDamage() - enemy:GetDamage()
local myHP = unit:GetMaxDamage() - unit:GetDamage()
-- Build promotion -> CS bonus lookup table
local promoBonuses = {{}}
pcall(function()
    for pm in GameInfo.UnitPromotionModifiers() do
        local mod = GameInfo.Modifiers[pm.ModifierId]
        if mod and mod.ModifierType == "MODIFIER_UNIT_ADJUST_COMBAT_STRENGTH" then
            for arg in GameInfo.ModifierArguments() do
                if arg.ModifierId == pm.ModifierId and arg.Name == "Amount" then
                    local val = tonumber(arg.Value) or 0
                    if val ~= 0 then
                        if not promoBonuses[pm.UnitPromotionType] then
                            promoBonuses[pm.UnitPromotionType] = {{}}
                        end
                        table.insert(promoBonuses[pm.UnitPromotionType], {{
                            amount = val,
                            name = pm.ModifierId
                        }})
                    end
                end
            end
        end
    end
end)
-- Sum promotion bonuses for a unit
local function getPromoBonuses(u)
    local total = 0
    local parts = {{}}
    local exp = u:GetExperience()
    for promoType, infos in pairs(promoBonuses) do
        local promoRow = GameInfo.UnitPromotions[promoType]
        if promoRow then
            local ok, has = pcall(function() return exp:HasPromotion(promoRow.Index) end)
            if ok and has then
                for _, info in ipairs(infos) do
                    total = total + info.amount
                    local short = info.name:gsub("MODIFIER_", "")
                    table.insert(parts, short .. " " .. (info.amount > 0 and "+" or "") .. info.amount)
                end
            end
        end
    end
    return total, parts
end
-- Gather modifiers
local mods = {{}}
local defModTotal = 0
local attModTotal = 0
-- Attacker promotion bonuses
local attPromoBonus, attPromoMods = getPromoBonuses(unit)
if attPromoBonus ~= 0 then
    attModTotal = attModTotal + attPromoBonus
    for _, m in ipairs(attPromoMods) do table.insert(mods, "att " .. m) end
end
-- Defender promotion bonuses
local defPromoBonus, defPromoMods = getPromoBonuses(enemy)
if defPromoBonus ~= 0 then
    defModTotal = defModTotal + defPromoBonus
    for _, m in ipairs(defPromoMods) do table.insert(mods, "def " .. m) end
end
-- Defender fortified?
local ok1, ft = pcall(function() return enemy:GetFortifyTurns() end)
if ok1 and ft and ft > 0 then
    local bonus = math.min(ft * 3, 6)
    table.insert(mods, "fortified +" .. bonus)
    defModTotal = defModTotal + bonus
end
-- Defender on hills?
local tgtPlot = Map.GetPlot({target_x}, {target_y})
if tgtPlot and tgtPlot:IsHills() then
    table.insert(mods, "hills +3")
    defModTotal = defModTotal + 3
end
-- Forest/jungle defense bonus
if tgtPlot then
    local feat = tgtPlot:GetFeatureType()
    if feat >= 0 then
        local fInfo = GameInfo.Features[feat]
        if fInfo and (fInfo.FeatureType == "FEATURE_FOREST" or fInfo.FeatureType == "FEATURE_JUNGLE") then
            table.insert(mods, fInfo.FeatureType:gsub("FEATURE_",""):lower() .. " +3")
            defModTotal = defModTotal + 3
        end
    end
end
-- River crossing penalty (attacker crosses river for melee)
if not isRanged and tgtPlot then
    local attPlot = Map.GetPlot(ux, uy)
    if attPlot and tgtPlot:IsRiverCrossingToPlot(attPlot) then
        table.insert(mods, "river -2")
        attModTotal = attModTotal - 2
    end
end
-- Flanking: count our units adjacent to defender (excluding attacker)
local flankBonus = 0
if not isRanged then
    local enemyOwner = enemy:GetOwner()
    for dy = -1, 1 do for dx = -1, 1 do
        if dx ~= 0 or dy ~= 0 then
            local fx, fy = {target_x} + dx, {target_y} + dy
            if not (fx == ux and fy == uy) then
                local adjUnits = Map.GetUnitsAt(fx, fy)
                if adjUnits then
                    for adjU in adjUnits:Units() do
                        if adjU:GetOwner() == me then
                            local adjInfo = GameInfo.Units[adjU:GetType()]
                            if adjInfo and (adjInfo.Combat or 0) > 0 then
                                flankBonus = flankBonus + 2
                            end
                        end
                    end
                end
            end
        end
    end end
    if flankBonus > 0 then
        table.insert(mods, "flank +" .. flankBonus)
        attModTotal = attModTotal + flankBonus
    end
end
-- Support: count defender's adjacent friendlies
local supportBonus = 0
if not isRanged then
    local enemyOwner = enemy:GetOwner()
    for dy = -1, 1 do for dx = -1, 1 do
        if dx ~= 0 or dy ~= 0 then
            local sx, sy = {target_x} + dx, {target_y} + dy
            local adjUnits = Map.GetUnitsAt(sx, sy)
            if adjUnits then
                for adjU in adjUnits:Units() do
                    if adjU:GetOwner() == enemyOwner and adjU ~= enemy then
                        local adjInfo = GameInfo.Units[adjU:GetType()]
                        if adjInfo and (adjInfo.Combat or 0) > 0 then
                            supportBonus = supportBonus + 2
                        end
                    end
                end
            end
        end
    end end
    if supportBonus > 0 then
        table.insert(mods, "support +" .. supportBonus)
        defModTotal = defModTotal + supportBonus
    end
end
local effDefCS = defCS + defModTotal
effAttCS = effAttCS + attModTotal
print("ESTIMATE|" .. attType .. "|" .. defType .. "|" .. effAttCS .. "|" .. effDefCS .. "|" .. (isRanged and "1" or "0") .. "|" .. table.concat(mods, ";") .. "|" .. myHP .. "|" .. enemyHP)
print("{SENTINEL}")
"""


def parse_combat_estimate(
    lines: list[str], att_cs: int, def_cs: int
) -> CombatEstimate | None:
    """Parse ESTIMATE line and compute damage using Civ 6 formula."""
    for line in lines:
        if line.startswith("ESTIMATE|"):
            p = line.split("|")
            if len(p) < 9:
                return None
            eff_att = int(p[3])
            eff_def = int(p[4])
            is_ranged = p[5] == "1"
            mods = [m for m in p[6].split(";") if m]
            my_hp = int(p[7])
            enemy_hp = int(p[8])
            # 残血战力衰减(2026-08-02 校准局 0e):Civ6 中受伤单位战力
            # 按伤势线性打折,满伤 -10。此前不折算 → 对 1 血守军仍报
            # "我方受伤 ~14"。折算量 = 10 × 已损血量/100(单位满血 100)。
            # 2026-08-10:城市目标没有"残血战力衰减"(那是单位机制),
            # 城血还是 200 上限 —— 拿 100-183 去算衰减纯属胡来。
            is_city = p[2].startswith("CITY ")
            att_wound = round(10 * max(0, 100 - my_hp) / 100)
            if att_wound:
                eff_att -= att_wound
                mods.append(f"att wounded -{att_wound}")
            if not is_city:
                def_wound = round(10 * max(0, 100 - enemy_hp) / 100)
                if def_wound:
                    eff_def -= def_wound
                    mods.append(f"def wounded -{def_wound}")
            # Civ 6 damage formula: BASE * 10^((att-def)/30)

            base_damage = 24
            if eff_att > 0 and eff_def > 0:
                dmg_to_def = base_damage * (10 ** ((eff_att - eff_def) / 30))
                dmg_to_att = (
                    base_damage * (10 ** ((eff_def - eff_att) / 30))
                    if not is_ranged
                    else 0
                )
            else:
                dmg_to_def = 0
                dmg_to_att = 0
            # 单次攻击伤害封顶 100(一个单位/一次攻击最多打掉满血)。
            # 2026-08-10 伦敦:未封顶时报出 "~1906 伤 → LIKELY KILL",
            # 那个数字本身就是"防御方选错了"的最大证据,却被当成了捷报。
            dmg_to_def = min(100.0, dmg_to_def)
            dmg_to_att = min(100.0, dmg_to_att)
            return CombatEstimate(
                attacker_type=p[1],
                defender_type=p[2],
                attacker_cs=eff_att,
                defender_cs=eff_def,
                is_ranged=is_ranged,
                modifiers=mods,
                est_damage_to_defender=int(round(dmg_to_def)),
                est_damage_to_attacker=int(round(dmg_to_att)),
                defender_hp=enemy_hp,
                attacker_hp=my_hp,
            )
    return None


def build_threat_scan_query() -> str:
    """GameCore: scan for foreign military units visible to the player.

    Scans all players (not just barbarians) but only reports units on tiles
    the player can currently see (PlayersVisibility:IsVisible). No arbitrary
    distance limits — fog of war is the natural filter.

    Uses GameCore context but filters by fog of war — only reports units
    on tiles the player can currently see (PlayersVisibility:IsVisible).
    Reports owner, HP, combat strength, and distance from nearest friendly position.
    """
    return (
        _LUA_HEX_DIST
        + """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local myPos = {}
-- 2026-08-04 P1:海军威胁建模。GALLEY 在海岸游走曾反复触发 RED 冻结
-- 平民生产,但悬崖 LOS 让它实际打不到任何城市。威胁等级需要知道:
-- ①我方是否有沿海城市(coast);②威胁距最近【城市】的距离(cd),
-- 而不是距最近单位/城市的混合距离。近战海军要贴脸(cd<=1)才有威胁,
-- 远程海军要 cd<=range+1;我方无沿海城市时海军永远不是 RED。
local myCoastal = 0
local myCityList = {}
for _, c in Players[me]:GetCities():Members() do
    table.insert(myPos, {c:GetX(), c:GetY()})
    table.insert(myCityList, {c:GetX(), c:GetY()})
    local cP = Map.GetPlot(c:GetX(), c:GetY())
    if cP and cP:IsCoastalLand() then myCoastal = 1 end
end
for _, u in Players[me]:GetUnits():Members() do
    local ux, uy = u:GetX(), u:GetY()
    if ux ~= -9999 then table.insert(myPos, {ux, uy}) end
end
local found = false
for pid = 0, 63 do
    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
        local isBarbarian = (pid == 63)
        -- 2026-08-02 校准局 4c:此前只过滤城邦,主要文明无论是否宣战一律
        -- 计入威胁 —— 中立国侦察兵在边境游走就触发满级红警,冻结平民
        -- 生产与探图。和平状态的单位不会攻击或俘虏:只有蛮族与已宣战方
        -- 才是威胁。
        if not isBarbarian and not pDiplo:IsAtWarWith(pid) then
            -- At peace (major civ or city-state) — not a threat
        else
        local ownerName = "Barbarian"
        if pid ~= 63 then
            local cfg = PlayerConfigurations[pid]
            if cfg then ownerName = Locale.Lookup(cfg:GetCivilizationShortDescription()) end
        end
        for _, bu in Players[pid]:GetUnits():Members() do
            local bx, by = bu:GetX(), bu:GetY()
            if bx ~= -9999 and pVis:IsVisible(bx, by) then
                local uType = bu:GetType()
                if uType then
                    local entry = GameInfo.Units[uType]
                    local bcs = entry and entry.Combat or 0
                    -- 攻城类的远程在 Bombard 列 —— 漏掉它,敌方投石机/攻城炮
                    -- 就不算军事单位,威胁扫描直接看不见它们(T291)。
                    local bombardVal = tonumber(entry and entry.Bombard) or 0
                    local rangedVal = tonumber(entry and entry.RangedCombat) or 0
                    if bcs > 0 or rangedVal > 0 or bombardVal > 0 then
                        local minDist = 999
                        for _, pos in ipairs(myPos) do
                            local d = hexDist(pos[1], pos[2], bx, by)
                            if d < minDist then minDist = d end
                        end
                        local cityDist = 999
                        for _, cpos in ipairs(myCityList) do
                            local d = hexDist(cpos[1], cpos[2], bx, by)
                            if d < cityDist then cityDist = d end
                        end
                        local name = entry and entry.UnitType or "UNKNOWN"
                        local hp = bu:GetMaxDamage() - bu:GetDamage()
                        local brs = math.max(rangedVal, bombardVal)
                        local isCS = Players[pid]:IsMajor() and "0" or "1"
                        local bmv = (entry and entry.BaseMoves) or 2
                        local brg = (entry and entry.Range) or 0
                        print("THREAT|" .. pid .. "|" .. ownerName:gsub("|","/") .. "|" .. name .. "|" .. bx .. "," .. by .. "|" .. hp .. "/" .. bu:GetMaxDamage() .. "|CS:" .. bcs .. "|RS:" .. brs .. "|dist:" .. minDist .. "|cs:" .. isCS .. "|uid:" .. bu:GetID() .. "|mv:" .. bmv .. "|rg:" .. brg .. "|dm:" .. tostring(entry and entry.Domain or "") .. "|cd:" .. cityDist .. "|coast:" .. myCoastal)
                        found = true
                    end
                end
            end
        end
        end -- close city-state skip if/else
    end -- close if pid alive
end -- close for pid
if not found then print("NO_THREATS") end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)
    )


def build_fortify_unit(unit_index: int) -> str:
    return f"""
{_lua_get_unit(unit_index)}
if unit:GetFortifyTurns() > 0 then
    print("OK:ALREADY_FORTIFIED|Fortify turns: " .. unit:GetFortifyTurns())
    print("{SENTINEL}"); return
end
if UnitManager.CanStartOperation(unit, UnitOperationTypes.FORTIFY, nil, true) then
    UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
    print("OK:FORTIFIED")
else
    -- 睡着的单位 CanStartOperation(FORTIFY) 恒为 false。以前这里直接掉进
    -- SLEEP 分支,于是「让前线火枪手进入防御姿态」实际结果是它继续睡:
    -- 没有 +6 防御加成,敌人贴脸也不会醒。先唤醒再下令。
    local woke = false
    if UnitManager.CanStartCommand(unit, UnitCommandTypes.WAKE, nil, false) then
        UnitManager.RequestCommand(unit, UnitCommandTypes.WAKE)
        woke = true
    end
    if woke then
        -- WAKE 下一帧才结算,同帧再问 CanStartOperation 拿到的还是旧答案,
        -- 所以这里不复查、直接发 FORTIFY(和 MOVE_TO 的异步处理同一套路)。
        UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
        print("OK:FORTIFIED|唤醒后进入防御姿态(该单位原本处于睡眠)")
    else
        local sleepOp = GameInfo.UnitOperations["UNITOPERATION_SLEEP"]
        if sleepOp and UnitManager.CanStartOperation(unit, sleepOp.Hash, nil, true) then
            UnitManager.RequestOperation(unit, sleepOp.Hash)
            print("OK:SLEEPING")
        else
            {_bail("ERR:CANNOT_FORTIFY|Unit cannot fortify or sleep")}
        end
    end
end
print("{SENTINEL}")
"""


def build_disable_auto_end_turn() -> str:
    """关掉游戏的「自动结束回合」(InGame context)。

    2026-08-11 罗马局 T228 实机:AutoEndTurn 默认是开的。skip_remaining_units
    把所有单位 FinishMoves 之后,游戏判定「玩家没有待办」→ **自己把回合结束了**。
    后果不是小事:

      · end_turn 从没被调用,那一回合的日记条目根本不存在(T228 就这么没的);
      · 下一次 end_turn 拿到的 ov.turn 已经是 229,于是 T228 的内容被写进
        T229 —— 日记与回合号整体错位,而错位不会自己被发现;
      · AI 回合在 agent 不知情时跑完,敌军移动和战斗结果全部没被观察到。

    日记是跨会话记忆的唯一载体,所以回合推进必须只有 end_turn 一条路径。
    只改运行时内存值,不调 SaveCheckpoint —— 不碰用户磁盘上的配置。
    """
    return f"""
local before = UserConfiguration.GetValue("AutoEndTurn")
if before ~= nil and before ~= 0 then
    UserConfiguration.SetValue("AutoEndTurn", 0)
    print("OK:AUTO_END_TURN_DISABLED|was:" .. tostring(before))
else
    print("OK:AUTO_END_TURN_ALREADY_OFF")
end
print("{SENTINEL}")
"""


def build_skip_unit(unit_index: int) -> str:
    """Skip a unit's turn (GameCore context — uses FinishMoves)."""
    return f"""
{_lua_get_unit_gamecore(unit_index)}
UnitManager.FinishMoves(unit)
print("OK:SKIPPED")
print("{SENTINEL}")
"""


def build_fortify_remaining_units() -> str:
    """Fortify/heal combat units with remaining moves (InGame context).

    Tries to fortify (or heal if damaged) combat units. Non-combat units
    and units that can't fortify are left for skip_remaining_units to handle.
    """
    return """
local me = Game.GetLocalPlayer()
local fortified = 0
local healed = 0
local healHash = GameInfo.UnitOperations["UNITOPERATION_HEAL"] and GameInfo.UnitOperations["UNITOPERATION_HEAL"].Hash
for _, unit in Players[me]:GetUnits():Members() do
    local x = unit:GetX()
    if x ~= -9999 and unit:GetMovesRemaining() > 0 then
        local info = GameInfo.Units[unit:GetType()]
        local isCombat = info and info.Combat > 0
        if isCombat then
            if unit:GetDamage() > 0 and healHash then
                local ok = pcall(function()
                    if UnitManager.CanStartOperation(unit, healHash, nil, true) then
                        UnitManager.RequestOperation(unit, healHash)
                        healed = healed + 1
                    end
                end)
            else
                local ok = pcall(function()
                    if UnitManager.CanStartOperation(unit, UnitOperationTypes.FORTIFY, nil, true) then
                        UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
                        fortified = fortified + 1
                    end
                end)
            end
        end
    end
end
print("OK:FORTIFIED|" .. fortified .. " fortified, " .. healed .. " healing")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_form_unit(unit_index: int, target_x: int, target_y: int, army: bool) -> str:
    """把目标格的同类型单位合编进本单位,组成军团/大军(InGame context)。

    2026-08-12 罗马 T384 实机:23 个军事单位全程没编过队 —— 因为整个
    工具层没有编队入口(FORM_CORPS/FORM_ARMY 在 lua 层零命中)。军团 +10、
    大军 +17 战力,是市政树白送的两次力量尖峰,同样的存量兵力合编完
    等于凭空涨一个时代的代差。

    规则:两个【相同类型】的单位才能合编,发起方吸收目标方。军团需
    民族主义、大军需动员 —— 缺市政时引擎的 CanStartCommand 会拒绝,
    错误里带上原因,不静默失败。
    """
    cmd = "UNITCOMMAND_FORM_ARMY" if army else "UNITCOMMAND_FORM_CORPS"
    label = "大军" if army else "军团"
    civic = "动员(Mobilization)" if army else "民族主义(Nationalism)"
    # ⚠️ 错误消息在 Python 层预构造:f-string 只插值最外层 {},
    # _bail("…{label}…") 里的花括号在字符串字面量内部,会原样进 Lua。
    # (写这个函数时就踩了一次,ruff 的 F841「civic 未使用」抓出来的。)
    no_cmd = f"ERR:NO_FORM_COMMAND|{cmd} 不在本规则集中 —— {label}编队不可用"
    no_partner = (
        f'"ERR:NO_PARTNER|({target_x},{target_y}) 上没有可合编的同类型单位。'
        f'本单位是 " .. myType .. " —— 合编要求两个【相同类型】的单位,'
        f'先把另一个同型单位移到那一格。"'
    )
    cannot = (
        f'"ERR:CANNOT_FORM|引擎拒绝{label}编队。常见原因:①未解锁{civic};'
        f"②两单位不在相邻/同格可达位置;③本回合已无移动力;"
        f'④该兵种不可编队(平民/支援)。"'
    )
    ok_msg = (
        f'"OK:FORMED|" .. myType .. " 已合编为{label}(+战力),'
        f'吸收了 ({target_x},{target_y}) 的同型单位"'
    )
    return f"""
{_lua_get_unit(unit_index)}
local myInfo = GameInfo.Units[unit:GetType()]
local myType = myInfo and myInfo.UnitType or "?"
local cmdRow = GameInfo.UnitCommands["{cmd}"]
if not cmdRow then
    {_bail(no_cmd)}
end
-- 目标格上找【同类型】己方单位(合编的硬性要求)
local tgtUnits = Map.GetUnitsAt({target_x}, {target_y})
local partner = nil
if tgtUnits then
    for other in tgtUnits:Units() do
        if other:GetOwner() == me and other:GetID() ~= unit:GetID() then
            local oInfo = GameInfo.Units[other:GetType()]
            if oInfo and oInfo.UnitType == myType then partner = other break end
        end
    end
end
if not partner then
    {_bail_lua(no_partner)}
end
local tParams = {{}}
tParams[UnitCommandTypes.PARAM_UNIT_PLAYER] = partner:GetOwner()
tParams[UnitCommandTypes.PARAM_UNIT_ID] = partner:GetID()
local can = UnitManager.CanStartCommand(unit, cmdRow.Hash, nil, tParams, true)
if not can then
    {_bail_lua(cannot)}
end
UnitManager.RequestCommand(unit, cmdRow.Hash, tParams)
print({ok_msg})
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_rebase(unit_index: int, target_x: int, target_y: int) -> str:
    """空军转场到另一个航空港/航母(InGame context)。

    2026-08-12 罗马局 T455-458:缺陷113 修完 attack 和 move 之后仍然缺这一半。
    空军的射程是 15 格,超出就【只能转场】—— 而工具层没有这个入口,整局四次
    转场全靠 run_lua 手发 UNITOPERATION_REBASE。表现是:轰炸机能打眼前的城,
    打不到半张地图外的城,而空军的全部价值恰恰在于机动。

    实测要点(T458):
      · Hash -1054550409,参数 PARAM_X/PARAM_Y 指向目标航空港/航母所在格
      · CanStartOperation 可预检,失败原因主要是【超出转场距离】——
        (60,51)→(104,47) 44 格被拒,分段到 (85,44) 就成。所以拒绝时要把
        "分段转场"写进错误里,否则调用方会以为目标非法。
      · 目标必须是【己方航空港或航母】,普通地块不行。
    """
    return f"""
{_lua_get_unit(unit_index)}
local uInfo = GameInfo.Units[unit:GetType()]
if uInfo ~= nil and tostring(uInfo.Domain or "") ~= "DOMAIN_AIR" then
    {_bail_lua('"ERR:NOT_AIR_UNIT|" .. Locale.Lookup(unit:GetName()) .. " 不是空军单位,rebase 只对 DOMAIN_AIR 有效。地面单位用 move,商人用 teleport。"')}
end
local opRow = GameInfo.UnitOperations["UNITOPERATION_REBASE"]
if not opRow then {_bail("ERR:NO_REBASE_OP|UNITOPERATION_REBASE not found")} end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = {target_x}
params[UnitOperationTypes.PARAM_Y] = {target_y}
local ok, can = pcall(function()
    return UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
end)
if not (ok and can) then
    local dist = Map.GetPlotDistance(unit:GetX(), unit:GetY(), {target_x}, {target_y})
    {_bail_lua(f'"ERR:CANNOT_REBASE|从 (" .. unit:GetX() .. "," .. unit:GetY() .. ") 转场到 ({target_x},{target_y}) 被拒,直线距离 " .. dist .. " 格。两个常见原因:①【超出单次转场距离】—— 实测 44 格被拒、20 格可行,太远就先转到中间的航空港【分段跳】;②目标不是己方航空港/航母 —— 转场只能落在这两种地方,普通地块不行。"')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
print("OK:REBASE|" .. Locale.Lookup(unit:GetName()) .. "|from " .. unit:GetX() .. "," .. unit:GetY() .. " -> {target_x},{target_y}")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_excavate(unit_index: int) -> str:
    """考古学家在脚下遗址开挖(InGame context)。

    2026-08-09 T304 实机:unit_action 没有 excavate 入口,考古学家的核心
    操作只能 run_lua 手发。挖掘瞬时完成消耗 1 充能,文物随后弹
    NOTIFICATION_CHOOSE_ARTIFACT_PLAYER 待选(end_turn 侧自动处理)。
    """
    return f"""
{_lua_get_unit(unit_index)}
local opRow = GameInfo.UnitOperations["UNITOPERATION_EXCAVATE"]
if not opRow then {_bail("ERR:NO_EXCAVATE_OP|UNITOPERATION_EXCAVATE not found")} end
local ok, can = pcall(function() return UnitManager.CanStartOperation(unit, opRow.Hash, nil, true) end)
if not (ok and can) then
    {_bail_lua('"ERR:CANNOT_EXCAVATE|Cannot excavate at (" .. unit:GetX() .. "," .. unit:GetY() .. ") — 需站在文物遗址(ANTIQUITY_SITE)上且有剩余移动力"')}
end
UnitManager.RequestOperation(unit, opRow.Hash)
print("OK:EXCAVATING|at " .. unit:GetX() .. "," .. unit:GetY() .. " — 文物出土后需选择归属(CHOOSE_ARTIFACT)")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_skip_remaining_units() -> str:
    """Skip all units with moves remaining (GameCore context — FinishMoves for each).

    2026-08-09 T278-T281 实机:带多回合 move 指令的单位(提督/两个建造者)被
    FinishMoves 抹掉挂起路径,连续两回合原地罚站,每回合都要人肉重发 move。
    有 queued destination 的单位不碰 —— 引擎回合开始会自动续走,它们也不算
    "在等指令"(IsReadyToSelect=false),不会卡回合提交。
    """
    return """
local me = Game.GetLocalPlayer()
local count = 0
local enroute = 0
for _, unit in Players[me]:GetUnits():Members() do
    local x = unit:GetX()
    if x ~= -9999 and unit:GetMovesRemaining() > 0 then
        local queued = nil
        pcall(function()
            local qx, qy = UnitManager.GetQueuedDestination(unit)
            if qx and qx >= 0 then queued = true end
        end)
        if queued then
            enroute = enroute + 1
        else
            UnitManager.FinishMoves(unit)
            count = count + 1
        end
    end
end
local tail = ""
if enroute > 0 then tail = " (" .. enroute .. " en-route units left moving)" end
print("OK:SKIPPED|" .. count .. " units" .. tail)
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_automate_explore(unit_index: int) -> str:
    """Automate a unit's exploration (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
local hash = GameInfo.UnitOperations["UNITOPERATION_AUTOMATE_EXPLORE"].Hash
if not UnitManager.CanStartOperation(unit, hash, nil, nil) then
    {_bail("ERR:CANNOT_AUTOMATE|Unit cannot auto-explore")}
end
UnitManager.RequestOperation(unit, hash, {{}})
print("OK:AUTOMATED|" .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_heal_unit(unit_index: int) -> str:
    """Fortify until healed (InGame context). Distinct from plain fortify."""
    return f"""
{_lua_get_unit(unit_index)}
local hp = unit:GetMaxDamage() - unit:GetDamage()
local maxHP = unit:GetMaxDamage()
if hp >= maxHP then {_bail_lua('"ERR:FULL_HP|Unit already at full health (" .. hp .. "/" .. maxHP .. ")"')} end
local healHash = GameInfo.UnitOperations["UNITOPERATION_HEAL"].Hash
if UnitManager.CanStartOperation(unit, healHash, nil, nil) then
    UnitManager.RequestOperation(unit, healHash, {{}})
    print("OK:HEALING|HP:" .. hp .. "/" .. maxHP)
else
    {_bail("ERR:CANNOT_HEAL|Unit cannot fortify-until-healed")}
end
print("{SENTINEL}")
"""


def build_alert_unit(unit_index: int) -> str:
    """Put unit on alert — sleeps but auto-wakes when enemy enters sight (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
if UnitManager.CanStartOperation(unit, UnitOperationTypes.ALERT, nil, nil) then
    UnitManager.RequestOperation(unit, UnitOperationTypes.ALERT, {{}})
    print("OK:ALERT|" .. unit:GetX() .. "," .. unit:GetY())
else
    {_bail("ERR:CANNOT_ALERT|Unit cannot be put on alert")}
end
print("{SENTINEL}")
"""


def build_sleep_unit(unit_index: int) -> str:
    """Put unit to sleep — stays until manually woken (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
local sleepHash = GameInfo.UnitOperations["UNITOPERATION_SLEEP"].Hash
if UnitManager.CanStartOperation(unit, sleepHash, nil, nil) then
    UnitManager.RequestOperation(unit, sleepHash, {{}})
    print("OK:SLEEPING|" .. unit:GetX() .. "," .. unit:GetY())
else
    {_bail("ERR:CANNOT_SLEEP|Unit cannot sleep")}
end
print("{SENTINEL}")
"""


def build_delete_unit(unit_index: int) -> str:
    """Delete (disband) a unit (InGame context)."""
    return f"""
{_lua_get_unit(unit_index)}
local unitInfo = GameInfo.Units[unit:GetType()]
local uName = unitInfo and unitInfo.UnitType or "UNKNOWN"
-- 2026-08-05: 记录被解散单位的位置到 ExposedMembers（FireTuner 全局在
-- 游戏进程内跨连接保留）。end_turn 的"单位被杀"检测据此过滤掉主动解散，
-- 避免把 delete 误报成阵亡（本局实机：解散弩手/加农炮被报"被杀"）。
if not ExposedMembers then ExposedMembers = {{}} end
if not ExposedMembers.MCPDeletedUnits then ExposedMembers.MCPDeletedUnits = {{}} end
table.insert(ExposedMembers.MCPDeletedUnits, tostring(unit:GetX()) .. "," .. tostring(unit:GetY()))
if UnitManager.CanStartCommand(unit, UnitCommandTypes.DELETE, true) then
    UnitManager.RequestCommand(unit, UnitCommandTypes.DELETE)
    print("OK:DELETED|" .. uName .. " at " .. unit:GetX() .. "," .. unit:GetY())
else
    table.remove(ExposedMembers.MCPDeletedUnits)
    {_bail("ERR:CANNOT_DELETE|Unit cannot be deleted")}
end
print("{SENTINEL}")
"""


def build_improve_tile(unit_index: int, improvement_name: str) -> str:
    """Build an improvement with a builder unit (InGame context).

    improvement_name is e.g. IMPROVEMENT_FARM, IMPROVEMENT_MINE, etc.
    """
    return f"""
{_lua_get_unit(unit_index)}
local imp = GameInfo.Improvements["{improvement_name}"]
if imp == nil then
    -- Feature removals (IMPROVEMENT_REMOVE_*) may not be in Improvements table.
    -- Try scanning by ImprovementType name in case indexed lookup fails.
    for row in GameInfo.Improvements() do
        if row.ImprovementType == "{improvement_name}" then imp = row; break end
    end
    if imp == nil then
        -- List all available improvements so the agent can find the correct name
        local available = {{}}
        local params0 = {{}}
        params0[UnitOperationTypes.PARAM_X] = unit:GetX()
        params0[UnitOperationTypes.PARAM_Y] = unit:GetY()
        for row in GameInfo.Improvements() do
            if row.Buildable then
                params0[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = row.Hash
                local ok2, canBuild2 = pcall(function()
                    return UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, params0)
                end)
                if ok2 and canBuild2 then table.insert(available, row.ImprovementType) end
            end
        end
        local hint = #available > 0 and ". Available here: " .. table.concat(available, ", ") or ""
        {_bail_lua(f'"ERR:IMPROVEMENT_NOT_FOUND|{improvement_name} not in game database" .. hint')}
    end
end
local plot = Map.GetPlot(unit:GetX(), unit:GetY())
if plot:GetOwner() ~= me then {_bail_lua('"ERR:NOT_YOUR_TERRITORY|Tile at " .. unit:GetX() .. "," .. unit:GetY() .. " is not in your territory"')} end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = unit:GetX()
params[UnitOperationTypes.PARAM_Y] = unit:GetY()
params[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
if plot:IsImprovementPillaged() then
    local repairHash = GameInfo.UnitOperations["UNITOPERATION_REPAIR"] and GameInfo.UnitOperations["UNITOPERATION_REPAIR"].Hash
    if repairHash then
        local rParams = {{}}
        rParams[UnitOperationTypes.PARAM_X] = unit:GetX()
        rParams[UnitOperationTypes.PARAM_Y] = unit:GetY()
        -- Include improvement type — REPAIR may need to know WHICH improvement to restore
        local impType = plot:GetImprovementType()
        if impType >= 0 then
            local impRow = GameInfo.Improvements[impType]
            if impRow then rParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = impRow.Hash end
        end
        local canRepair = UnitManager.CanStartOperation(unit, repairHash, nil, rParams)
        if canRepair then
            UnitManager.RequestOperation(unit, repairHash, rParams)
            print("OK:REPAIRING|{improvement_name}|" .. unit:GetX() .. "," .. unit:GetY())
            print("{SENTINEL}"); return
        else
            -- CanStartOperation is unreliable (stale InGame state) — attempt anyway
            pcall(function() UnitManager.RequestOperation(unit, repairHash, rParams) end)
            -- Check if it worked by re-reading pillage state next frame
            print("WARN:REPAIR_ATTEMPTED|CanStartOperation=false but RequestOperation sent. Verify next turn.")
            print("{SENTINEL}"); return
        end
    end
end
if unit:GetMovesRemaining() <= 0 then
    print("ERR:CANNOT_IMPROVE|Builder has no moves remaining this turn")
    print("{SENTINEL}"); return
end
local canBuild, opResult = UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, params, true)
if not canBuild then
    -- Decode engine failure reasons. The results table is keyed by HASH
    -- (UnitOperationResults.FAILURE_REASONS), NOT by the string
    -- "FailureReasons" — reading the wrong key made these failures look
    -- like silent engine rejections.
    local reasons = {{}}
    if opResult and UnitOperationResults and UnitOperationResults.FAILURE_REASONS then
        local frs = opResult[UnitOperationResults.FAILURE_REASONS]
        if frs then
            for _, r in ipairs(frs) do
                local txt = tostring(r)
                pcall(function() txt = Locale.Lookup(txt) end)
                if txt and txt ~= "" then table.insert(reasons, (txt:gsub("|", "/"))) end
            end
        end
    end
    -- Tile context so the agent understands the terrain constraint
    local ctx = ""
    pcall(function()
        local tInfo = GameInfo.Terrains[plot:GetTerrainType()]
        ctx = "terrain=" .. (tInfo and tInfo.TerrainType or "?")
        local ft = plot:GetFeatureType()
        if ft >= 0 then
            local fInfo = GameInfo.Features[ft]
            ctx = ctx .. " feature=" .. (fInfo and fInfo.FeatureType or "?")
        end
        local rt = plot:GetResourceType()
        if rt >= 0 then
            local rInfo = GameInfo.Resources[rt]
            ctx = ctx .. " resource=" .. (rInfo and rInfo.ResourceType or "?")
        end
    end)
    -- What CAN be built on this tile right now?
    local avail = {{}}
    for row in GameInfo.Improvements() do
        if row.Buildable then
            local p2 = {{}}
            p2[UnitOperationTypes.PARAM_X] = unit:GetX()
            p2[UnitOperationTypes.PARAM_Y] = unit:GetY()
            p2[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = row.Hash
            local ok2, can2 = pcall(function()
                return UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, p2)
            end)
            if ok2 and can2 then table.insert(avail, row.ImprovementType) end
        end
    end
    local msg = "ERR:CANNOT_IMPROVE|{improvement_name} is not valid at " .. unit:GetX() .. "," .. unit:GetY()
    if #reasons > 0 then msg = msg .. " — " .. table.concat(reasons, "; ") end
    if ctx ~= "" then msg = msg .. " [" .. ctx .. "]" end
    if #avail > 0 then
        msg = msg .. " Buildable HERE now: " .. table.concat(avail, ", ") .. "."
    else
        msg = msg .. " NOTHING is buildable on this tile now (missing tech/civic, wrong terrain, or a feature must be removed first). Move the builder or research the required tech."
    end
    print(msg)
    print("{SENTINEL}"); return
end
local ok, reqErr = pcall(function()
    UnitManager.RequestOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, params)
end)
if not ok then
    print("ERR:REQUEST_OP_FAILED|" .. tostring(reqErr))
    print("{SENTINEL}"); return
end
print("OK:IMPROVING|{improvement_name}|" .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_remove_feature(unit_index: int) -> str:
    """Remove (chop/harvest) a feature from the tile the builder is standing on.

    Uses UNITOPERATION_REMOVE_FEATURE — works on forest, jungle, marsh.
    The game auto-detects which feature is present; no feature param needed.
    """
    return f"""
{_lua_get_unit(unit_index)}
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(unit:GetX(), unit:GetY())
local fType = plot:GetFeatureType()
if fType < 0 then
    {_bail_lua('"ERR:NO_FEATURE|No feature on tile (" .. unit:GetX() .. "," .. unit:GetY() .. ") to remove"')}
end
local fInfo = GameInfo.Features[fType]
local fName = fInfo and fInfo.FeatureType or "UNKNOWN"
local opRow = GameInfo.UnitOperations["UNITOPERATION_REMOVE_FEATURE"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REMOVE_FEATURE not available")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = unit:GetX()
params[UnitOperationTypes.PARAM_Y] = unit:GetY()
local canStart, opResult = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    -- Decode engine failure reasons (hash-keyed results table)
    local reasons = {{}}
    if opResult and UnitOperationResults and UnitOperationResults.FAILURE_REASONS then
        local frs = opResult[UnitOperationResults.FAILURE_REASONS]
        if frs then
            for _, r in ipairs(frs) do
                local txt = tostring(r)
                pcall(function() txt = Locale.Lookup(txt) end)
                if txt and txt ~= "" then table.insert(reasons, (txt:gsub("|", "/"))) end
            end
        end
    end
    if #reasons > 0 then
        {_bail_lua('"ERR:CANNOT_REMOVE_FEATURE|Cannot remove " .. fName .. " at " .. unit:GetX() .. "," .. unit:GetY() .. " — " .. table.concat(reasons, "; ")')}
    end
    {_bail_lua('"ERR:CANNOT_REMOVE_FEATURE|Cannot remove " .. fName .. " at " .. unit:GetX() .. "," .. unit:GetY() .. " (no reason given — likely missing tech: forest needs Mining, jungle needs Bronze Working, marsh needs Irrigation)"')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
print("OK:REMOVING_FEATURE|" .. fName .. " at " .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_repair_improvement(unit_index: int) -> str:
    """Repair a pillaged improvement at the builder's current tile (InGame context).

    Auto-detects the pillaged improvement — no improvement name needed.
    """
    return f"""
{_lua_get_unit(unit_index)}
local ux, uy = unit:GetX(), unit:GetY()
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(ux, uy)
if not plot then {_bail("ERR:NO_PLOT|Invalid plot")} end
local impType = plot:GetImprovementType()
if impType < 0 then
    {_bail_lua('"ERR:NO_IMPROVEMENT|No improvement on tile (" .. ux .. "," .. uy .. ") to repair"')}
end
local okPil, isPillaged = pcall(function() return plot:IsImprovementPillaged() end)
if not okPil or not isPillaged then
    local impInfo = GameInfo.Improvements[impType]
    local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
    {_bail_lua('"ERR:NOT_PILLAGED|" .. impName .. " at (" .. ux .. "," .. uy .. ") is not pillaged"')}
end
local impInfo = GameInfo.Improvements[impType]
local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
local repairOp = GameInfo.UnitOperations["UNITOPERATION_REPAIR"]
if not repairOp then {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REPAIR not available")} end
local rParams = {{}}
rParams[UnitOperationTypes.PARAM_X] = ux
rParams[UnitOperationTypes.PARAM_Y] = uy
if impInfo then rParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = impInfo.Hash end
local canRepair, repResult = UnitManager.CanStartOperation(unit, repairOp.Hash, nil, rParams, true)
if canRepair then
    UnitManager.RequestOperation(unit, repairOp.Hash, rParams)
    print("OK:REPAIRING|" .. impName .. " at (" .. ux .. "," .. uy .. ")")
else
    local reasons = {{}}
    if repResult and UnitOperationResults and UnitOperationResults.FAILURE_REASONS then
        local frs = repResult[UnitOperationResults.FAILURE_REASONS]
        if frs then
            for _, r in ipairs(frs) do
                local txt = tostring(r)
                pcall(function() txt = Locale.Lookup(txt) end)
                if txt and txt ~= "" then table.insert(reasons, (txt:gsub("|", "/"))) end
            end
        end
    end
    if #reasons > 0 then
        {_bail_lua('"ERR:CANNOT_REPAIR|" .. impName .. " at (" .. ux .. "," .. uy .. ") — " .. table.concat(reasons, "; ")')}
    end
    pcall(function() UnitManager.RequestOperation(unit, repairOp.Hash, rParams) end)
    print("WARN:REPAIR_ATTEMPTED|CanStartOperation=false with no stated reason; RequestOperation sent for " .. impName .. " at (" .. ux .. "," .. uy .. "). Verify next turn (charges should decrease).")
end
print("{SENTINEL}")
"""


def build_remove_improvement(unit_index: int) -> str:
    """Remove (demolish) an intact improvement from the builder's current tile.

    Uses UNITOPERATION_REMOVE_IMPROVEMENT. The game auto-detects which
    improvement is present; no improvement param needed. Costs one builder charge.
    """
    return f"""
{_lua_get_unit(unit_index)}
local ux, uy = unit:GetX(), unit:GetY()
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(ux, uy)
if not plot then {_bail("ERR:NO_PLOT|Invalid plot")} end
local impType = plot:GetImprovementType()
if impType < 0 then
    {_bail_lua('"ERR:NO_IMPROVEMENT|No improvement on tile (" .. ux .. "," .. uy .. ") to remove"')}
end
local impInfo = GameInfo.Improvements[impType]
local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
local opRow = GameInfo.UnitOperations["UNITOPERATION_REMOVE_IMPROVEMENT"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REMOVE_IMPROVEMENT not available in this game version")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = ux
params[UnitOperationTypes.PARAM_Y] = uy
local canStart = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    print("WARN:CANNOT_START|Attempting RequestOperation anyway.")
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
print("OK:REMOVING_IMPROVEMENT|" .. impName .. " at (" .. ux .. "," .. uy .. ")")
print("{SENTINEL}")
"""


def build_sacrifice_builder_charges(unit_index: int) -> str:
    """Sacrifice builder charges to boost a district project (Royal Society).

    Requires the Royal Society (BUILDING_GOV_SCIENCE) to be built.
    Builder must be on the district tile where a project is actively building.
    Consumes ALL remaining charges. Once per city per turn.
    Each charge adds 2% of the project's production cost.
    """
    return f"""
{_lua_get_unit(unit_index)}
local entry = GameInfo.Units[unit:GetType()]
if not entry or entry.UnitType ~= "UNIT_BUILDER" then {_bail("ERR:NOT_A_BUILDER|Unit is not a builder")} end
local ux, uy = unit:GetX(), unit:GetY()
local charges = unit:GetBuildCharges()
if charges <= 0 then {_bail("ERR:NO_CHARGES|Builder has no charges remaining")} end
if unit:GetMovesRemaining() <= 0 then {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")} end
-- Verify Royal Society exists
local hasRS = false
local rsIdx = GameInfo.Buildings["BUILDING_GOV_SCIENCE"] and GameInfo.Buildings["BUILDING_GOV_SCIENCE"].Index
if rsIdx then
    for _, city in Players[me]:GetCities():Members() do
        if city:GetBuildings():HasBuilding(rsIdx) then hasRS = true; break end
    end
end
if not hasRS then {_bail("ERR:NO_ROYAL_SOCIETY|Royal Society (Tier 3 government building) required")} end
-- Check builder is on a district tile
local plot = Map.GetPlot(ux, uy)
local distType = plot:GetDistrictType()
if distType < 0 then
    {_bail_lua('"ERR:NOT_ON_DISTRICT|Builder at (" .. ux .. "," .. uy .. ") is not on a district tile. Move to the district with an active project."')}
end
local dInfo = GameInfo.Districts[distType]
local dName = dInfo and dInfo.DistrictType or "UNKNOWN"
-- Find the city owning this plot and check for active project
local cityOwner = nil
for _, city in Players[me]:GetCities():Members() do
    for _, d in city:GetDistricts():Members() do
        if d:GetX() == ux and d:GetY() == uy then cityOwner = city; break end
    end
    if cityOwner then break end
end
if not cityOwner then {_bail("ERR:NO_CITY|Could not find city owning this district")} end
local bq = cityOwner:GetBuildQueue()
local producing = "nothing"
local okProd, currentHash = pcall(function() return bq:GetCurrentProductionTypeHash() end)
if okProd and currentHash then
    for proj in GameInfo.Projects() do
        if proj.Hash == currentHash then producing = proj.ProjectType; break end
    end
end
if producing == "nothing" then
    {_bail_lua('"ERR:NO_PROJECT|" .. Locale.Lookup(cityOwner:GetName()) .. " is not building a project. Queue a project first."')}
end
-- Execute the command
local cmdRow = GameInfo.UnitCommands["UNITCOMMAND_PROJECT_PRODUCTION"]
if not cmdRow then {_bail("ERR:CMD_UNAVAILABLE|UNITCOMMAND_PROJECT_PRODUCTION is not in this ruleset. Royal Society project boosting requires an expansion (Rise and Fall / Gathering Storm); this game is RULESET_STANDARD. Use action='hurry_production' for the wonder/district equivalents.")} end
local cmdHash = cmdRow.Hash
local can, failTable = UnitManager.CanStartCommand(unit, cmdHash, nil, true)
if not can then
    local reasons = {{}}
    if failTable then
        for _, v in pairs(failTable) do
            if type(v) == "table" then
                for _, s in pairs(v) do
                    if type(s) == "string" and s ~= "" then table.insert(reasons, s) end
                end
            end
        end
    end
    local reasonStr = #reasons > 0 and table.concat(reasons, "; ") or "unknown"
    {_bail_lua('"ERR:CANNOT_SACRIFICE|" .. reasonStr .. ". Builder at (" .. ux .. "," .. uy .. ") on " .. dName .. " with " .. charges .. " charges, city building " .. producing')}
end
-- Try with coordinate params first
local tParams = {{}}
tParams[UnitCommandTypes.PARAM_X] = ux
tParams[UnitCommandTypes.PARAM_Y] = uy
UnitManager.RequestCommand(unit, cmdHash, tParams)
-- Verify charges were consumed
local newCharges = unit:GetBuildCharges()
if newCharges == charges then
    -- Fallback: try with empty params
    UnitManager.RequestCommand(unit, cmdHash, {{}})
    newCharges = unit:GetBuildCharges()
end
if newCharges == charges then
    -- Second fallback: try RequestCommandImmediate
    pcall(function() UnitManager.RequestCommandImmediate(unit, cmdHash, tParams) end)
    newCharges = unit:GetBuildCharges()
end
if newCharges < charges then
    local consumed = charges - newCharges
    print("OK:SACRIFICED|" .. consumed .. " charges consumed for " .. producing .. " in " .. Locale.Lookup(cityOwner:GetName()) .. " at (" .. ux .. "," .. uy .. ") on " .. dName)
else
    print("WARN:SACRIFICE_UNCERTAIN|Command sent but charges unchanged (" .. charges .. "). Builder at (" .. ux .. "," .. uy .. ") on " .. dName .. ", city building " .. producing .. ". Ensure builder is on the exact district tile where the project's district is located.")
end
print("{SENTINEL}")
"""


# Builder charge-spending commands, in auto-detection order.
# kind -> (UnitCommand type, TraitModifier that grants it; "" = ungated)
_HURRY_COMMANDS: dict[str, tuple[str, str]] = {
    "wonder": ("UNITCOMMAND_WONDER_PRODUCTION", "TRAIT_BUILDER_WONDER_PERCENT"),
    "district": ("UNITCOMMAND_DISTRICT_PRODUCTION", "TRAIT_BUILDER_DISTRICT_PERCENT"),
    "project": ("UNITCOMMAND_PROJECT_PRODUCTION", ""),
}


def build_hurry_production(unit_index: int, kind: str = "auto") -> str:
    """Spend builder charges to hurry construction on the builder's tile.

    Covers the three charge-spending commands that share one shape:
        wonder   — Qin Shi Huang (FIRST_EMPEROR_TRAIT), early-era wonders only
        district — Aztec (TRAIT_CIVILIZATION_LEGEND_FIVE_SUNS)
        project  — Royal Society, expansion-only (absent in RULESET_STANDARD)

    kind='auto' probes each in turn and uses the first the engine accepts.
    When none is available the failure lists, per kind, whether the command
    exists in the ruleset, whether this leader/civ was granted the ability,
    and what the engine said — so a refusal is self-explaining rather than
    a bare "cannot start".
    """
    requested = kind.lower()
    if requested not in ("auto", *_HURRY_COMMANDS):
        valid = ", ".join(("auto", *_HURRY_COMMANDS))
        return f'print("ERR:BAD_KIND|Unknown kind \'{kind}\'. Valid: {valid}"); print("{SENTINEL}")'
    order = list(_HURRY_COMMANDS) if requested == "auto" else [requested]
    cand_lua = ",\n    ".join(
        '{kind="%s", cmd="%s", mod="%s"}'
        % (k, _HURRY_COMMANDS[k][0], _HURRY_COMMANDS[k][1])
        for k in order
    )

    return f"""
{_lua_get_unit(unit_index)}
local entry = GameInfo.Units[unit:GetType()]
local uType = entry and entry.UnitType or "UNKNOWN"
local ux, uy = unit:GetX(), unit:GetY()
local charges = unit:GetBuildCharges()
if charges <= 0 then {_bail_lua('"ERR:NO_CHARGES|" .. uType .. " has no build charges to spend"')} end
if unit:GetMovesRemaining() <= 0 then {_bail("ERR:NO_MOVES|Unit has no moves remaining this turn")} end

-- Which traits does this player have? Used only to EXPLAIN a refusal --
-- CanStartCommand below is the authority, so a failed lookup here can
-- never turn into a false "you lack the ability".
local civT, ldrT = "UNKNOWN_CIV", "UNKNOWN_LEADER"
pcall(function()
    local cfg = PlayerConfigurations[me]
    civT = cfg:GetCivilizationTypeName() or civT
    ldrT = cfg:GetLeaderTypeName() or ldrT
end)
local myTraits = {{}}
pcall(function()
    for r in GameInfo.CivilizationTraits() do
        if r.CivilizationType == civT then myTraits[r.TraitType] = true end
    end
    for r in GameInfo.LeaderTraits() do
        if r.LeaderType == ldrT then myTraits[r.TraitType] = true end
    end
end)
-- Resolve a TraitModifier to its owning traits, and whether we hold one.
local function grantedBy(modId)
    local owners, mine = {{}}, false
    for r in GameInfo.TraitModifiers() do
        if r.ModifierId == modId then
            table.insert(owners, r.TraitType)
            if myTraits[r.TraitType] then mine = true end
        end
    end
    return mine, (#owners > 0 and table.concat(owners, "/") or "unknown trait")
end

-- Game.GetEras() is absent in both Lua contexts here; the player object works.
-- MaxEra on UnitCommands compares against ChronologyIndex (Classical = 2),
-- not Eras.Index, so report both rather than assuming the comparison.
local curEraName, curEraChrono = "UNKNOWN_ERA", -1
pcall(function()
    local eraRow = GameInfo.Eras[Players[me]:GetEra()]
    if eraRow then
        curEraName = eraRow.EraType
        curEraChrono = eraRow.ChronologyIndex
    end
end)

local cands = {{
    {cand_lua}
}}

local chosen, chosenKind = nil, nil
local reasons = {{}}
for _, c in ipairs(cands) do
    local row = GameInfo.UnitCommands[c.cmd]
    if not row then
        table.insert(reasons, c.kind .. ": " .. c.cmd ..
            " absent from this ruleset (expansion content)")
    else
        local ok, can = pcall(function()
            return UnitManager.CanStartCommand(unit, row.Hash, nil, true)
        end)
        if ok and can then
            chosen, chosenKind = row, c.kind
            break
        end
        -- Refused. Work out WHY so the message is actionable.
        local why = nil
        if c.mod ~= "" then
            local mine, owners = grantedBy(c.mod)
            if not mine then
                why = " ability not granted to " .. tostring(ldrT) .. " / " ..
                      tostring(civT) .. " (comes from " .. tostring(owners) .. ")"
            end
        end
        if not why then
            why = " unit must stand on the tile where construction is in progress"
        end
        local eraNote = ""
        if row.MaxEra and row.MaxEra >= 0 then
            eraNote = " [command MaxEra=" .. tostring(row.MaxEra) .. "; current " ..
                      tostring(curEraName) .. " chronology=" .. tostring(curEraChrono) .. "]"
        end
        table.insert(reasons, c.kind .. ": engine refused at (" ..
            tostring(ux) .. "," .. tostring(uy) .. ")" .. eraNote .. " —" .. why)
    end
end

if not chosen then
    -- Point the user at where a builder could actually help.
    local targets = {{}}
    for _, city in Players[me]:GetCities():Members() do
        local bq = city:GetBuildQueue()
        local hash = nil
        pcall(function() hash = bq:GetCurrentProductionTypeHash() end)
        if hash then
            for b in GameInfo.Buildings() do
                if b.Hash == hash and b.IsWonder then
                    table.insert(targets, Locale.Lookup(city:GetName()) ..
                        " building wonder " .. b.BuildingType)
                    break
                end
            end
            for d in GameInfo.Districts() do
                if d.Hash == hash then
                    table.insert(targets, Locale.Lookup(city:GetName()) ..
                        " building district " .. d.DistrictType)
                    break
                end
            end
        end
    end
    local hint = #targets > 0
        and (" In progress now: " .. table.concat(targets, "; ") .. ".")
        or " No city is currently building a wonder or district."
    {_bail_lua('"ERR:HURRY_UNAVAILABLE|" .. table.concat(reasons, " | ") .. "." .. hint')}
end

local tParams = {{}}
tParams[UnitCommandTypes.PARAM_X] = ux
tParams[UnitCommandTypes.PARAM_Y] = uy
UnitManager.RequestCommand(unit, chosen.Hash, tParams)
local newCharges = unit:GetBuildCharges()
if newCharges == charges then
    UnitManager.RequestCommand(unit, chosen.Hash, {{}})
    newCharges = unit:GetBuildCharges()
end
if newCharges == charges then
    pcall(function() UnitManager.RequestCommandImmediate(unit, chosen.Hash, tParams) end)
    newCharges = unit:GetBuildCharges()
end

if newCharges < charges then
    print("OK:HURRIED|" .. chosenKind .. "|" .. (charges - newCharges) ..
          " charges spent by " .. uType .. " at (" .. ux .. "," .. uy ..
          "), " .. newCharges .. " remaining")
else
    print("WARN:HURRY_UNCERTAIN|" .. chosenKind ..
          " command accepted by CanStartCommand but charges unchanged (" .. charges ..
          ") for " .. uType .. " at (" .. ux .. "," .. uy ..
          "). Verify the construction tile and re-check the unit.")
end
print("{SENTINEL}")
"""


def build_build_route(unit_index: int) -> str:
    """Build a route (road/railroad) on the Military Engineer's current tile.

    Uses UNITOPERATION_BUILD_ROUTE — after Steam Power tech this builds
    railroads (route type 4).  Does NOT consume charges.  Costs 1 Iron +
    1 Coal per railroad tile from the player's stockpile.
    """
    return f"""
{_lua_get_unit(unit_index)}
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Military Engineer has no moves remaining this turn")}
end
local x, y = unit:GetX(), unit:GetY()
local plot = Map.GetPlot(x, y)
if not plot or plot:GetOwner() ~= me then
    {_bail_lua('"ERR:NOT_YOUR_TERRITORY|Tile (" .. x .. "," .. y .. ") is not in your territory"')}
end
local opRow = GameInfo.UnitOperations["UNITOPERATION_BUILD_ROUTE"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_BUILD_ROUTE not in game database")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = x
params[UnitOperationTypes.PARAM_Y] = y
local canStart = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    local rt = plot:GetRouteType()
    local reason = "unknown reason"
    if rt == 4 then reason = "tile already has a railroad"
    elseif plot:IsCity() then reason = "cannot build on city center"
    end
    {_bail_lua('"ERR:CANNOT_BUILD_ROUTE|" .. reason .. " at (" .. x .. "," .. y .. ")"')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
-- Read back route type (may be stale same-frame, but try)
local newRoute = plot:GetRouteType()
local routeName = "ROUTE"
if newRoute == 4 then routeName = "RAILROAD"
elseif newRoute >= 0 then routeName = "ROAD"
end
print("OK:BUILT_" .. routeName .. "|" .. x .. "," .. y)
print("{SENTINEL}")
"""


def parse_blocking_sessions(lines: list[str]) -> str:
    """从 get_units 的输出里提取阻塞性外交/交易会话提示。没有返回空串。

    2026-08-07 用户实机报告:AI 弹出「领袖头像+交易内容」窗口时工具识别
    不到,后续动作全部静默无效,整局卡住。检测能力一直有,但只挂在
    end_turn 的超时分支上 —— 回合中途弹出来的窗口无人过问。
    """
    for line in lines:
        if not line.startswith("BLOCKER|"):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            dip_n, dip_who, deal_n, deal_who = (
                int(parts[1]),
                parts[2],
                int(parts[3]),
                parts[4],
            )
        except (TypeError, ValueError):
            continue
        out = [
            "🚫 有窗口挡着 —— 在处理掉之前,你发出的任何单位/城市命令都可能"
            "静默失效(照样返回成功,实际什么都没发生)。"
        ]
        if dip_n:
            out.append(
                f"   · {dip_n} 个未关闭的外交会话:{dip_who}"
                f" → respond_to_diplomacy(other_player_id=<#后面的数字>, response='NEGATIVE')"
                f" 或 response='EXIT' 强制关闭"
            )
        if deal_n:
            out.append(
                f"   · {deal_n} 个待处理的交易提案:{deal_who}"
                f" → get_pending_trades 看内容,再 respond_to_trade(other_player_id=<#后面的数字>, accept=True/False)"
            )
        out.append("   · 都不奏效时用 dismiss_popup 兜底。")
        return chr(10).join(out)
    return ""


def parse_units_response(lines: list[str]) -> list[UnitInfo]:
    units = []
    for line in lines:
        if line.startswith("BLOCKER|"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        x_str, y_str = parts[4].split(",")
        moves_cur, moves_max = parts[5].split("/")
        hp_cur, hp_max = parts[6].split("/")
        cs = int(parts[7]) if len(parts) > 7 else 0
        rs = int(parts[8]) if len(parts) > 8 else 0
        charges = int(parts[9]) if len(parts) > 9 else 0
        targets_raw = parts[10] if len(parts) > 10 else ""
        targets = [t for t in targets_raw.split(";") if t] if targets_raw else []
        needs_promo = parts[11] == "1" if len(parts) > 11 else False
        can_upgrade = parts[12] == "1" if len(parts) > 12 else False
        upgrade_target = parts[13] if len(parts) > 13 else ""
        upgrade_cost = int(parts[14]) if len(parts) > 14 and parts[14].isdigit() else 0
        valid_imps_raw = parts[15] if len(parts) > 15 else ""
        valid_imps = (
            [v for v in valid_imps_raw.split(";") if v] if valid_imps_raw else []
        )
        religion = parts[16] if len(parts) > 16 else ""
        # 缺字段时默认 True(需要指令)—— 见 UnitInfo.needs_orders 的说明
        needs_orders = parts[17] == "1" if len(parts) > 17 else True
        units.append(
            UnitInfo(
                unit_id=int(parts[0]),
                unit_index=int(parts[1]),
                name=parts[2],
                unit_type=parts[3],
                x=int(x_str),
                y=int(y_str),
                moves_remaining=float(moves_cur),
                max_moves=float(moves_max),
                health=int(hp_cur),
                max_health=int(hp_max),
                combat_strength=cs,
                ranged_strength=rs,
                build_charges=charges,
                targets=targets,
                needs_promotion=needs_promo,
                can_upgrade=can_upgrade,
                upgrade_target=upgrade_target,
                upgrade_cost=upgrade_cost,
                valid_improvements=valid_imps,
                religion=religion,
                needs_orders=needs_orders,
            )
        )
    return units


def parse_threat_scan_response(lines: list[str]) -> list[ThreatInfo]:
    threats: list[ThreatInfo] = []
    for line in lines:
        if not line.startswith("THREAT|"):
            continue
        parts = line.split("|")
        # Format: THREAT|owner_id|owner_name|unit_type|x,y|hp/max|CS:n|RS:n|dist:n|cs:0/1|uid:N
        if len(parts) >= 9:
            x_str, y_str = parts[4].split(",")
            hp_str, max_str = parts[5].split("/")
            cs = int(parts[6].replace("CS:", "")) if parts[6].startswith("CS:") else 0
            rs = int(parts[7].replace("RS:", "")) if parts[7].startswith("RS:") else 0
            dist = (
                int(parts[8].replace("dist:", ""))
                if parts[8].startswith("dist:")
                else 0
            )
            uid = 0
            if len(parts) > 10 and parts[10].startswith("uid:"):
                uid = int(parts[10][4:])
            # 对手推演用的机动数据。老格式没有这两段 → 保守兜底 2/0。
            mv, rg = 2, 0
            # 2026-08-04 P1:海军威胁字段(dm/cd/coast)。老格式缺失时
            # 保守兜底:陆地单位 + 无城市距离信息(999) + 无沿海城市,
            # 这样海军误报规则不会把老格式数据当成威胁。
            domain, cd, coast = "DOMAIN_LAND", 999, 0
            for p in parts[11:]:
                if p.startswith("mv:"):
                    mv = int(p[3:] or 2)
                elif p.startswith("rg:"):
                    rg = int(p[3:] or 0)
                elif p.startswith("dm:"):
                    domain = p[3:] or "DOMAIN_LAND"
                elif p.startswith("cd:"):
                    cd = int(p[3:] or 999)
                elif p.startswith("coast:"):
                    coast = int(p[6:] or 0)
            threats.append(
                ThreatInfo(
                    unit_type=parts[3],
                    x=int(x_str),
                    y=int(y_str),
                    hp=int(hp_str),
                    max_hp=int(max_str),
                    combat_strength=cs,
                    ranged_strength=rs,
                    distance=dist,
                    owner_id=int(parts[1]),
                    owner_name=parts[2],
                    is_city_state=len(parts) > 9
                    and parts[9].startswith("cs:")
                    and parts[9][3:] == "1",
                    unit_id=uid,
                    base_moves=mv,
                    range=rg,
                    domain=domain,
                    city_distance=cd,
                    has_coastal_city=coast == 1,
                )
            )
        elif len(parts) >= 7:
            # Legacy format fallback: THREAT|unit_type|x,y|hp/max|CS:n|RS:n|dist:n
            x_str, y_str = parts[2].split(",")
            hp_str, max_str = parts[3].split("/")
            cs = int(parts[4].replace("CS:", "")) if parts[4].startswith("CS:") else 0
            rs = int(parts[5].replace("RS:", "")) if parts[5].startswith("RS:") else 0
            dist = (
                int(parts[6].replace("dist:", ""))
                if parts[6].startswith("dist:")
                else 0
            )
            threats.append(
                ThreatInfo(
                    unit_type=parts[1],
                    x=int(x_str),
                    y=int(y_str),
                    hp=int(hp_str),
                    max_hp=int(max_str),
                    combat_strength=cs,
                    ranged_strength=rs,
                    distance=dist,
                )
            )
    return threats


def build_fog_neighbor_query(positions: list[tuple[int, int]]) -> str:
    """GameCore: for each position, report which adjacent tiles are in fog."""
    checks = "\n".join(f"check({x},{y})" for x, y in positions)
    return f"""
local me = Game.GetLocalPlayer()
local pVis = PlayersVisibility[me]
local dirNames = {{"NE","E","SE","SW","W","NW"}}
function check(cx, cy)
    local plot = Map.GetPlot(cx, cy)
    if not plot then return end
    local fog = {{}}
    for i = 0, 5 do
        local adj = Map.GetAdjacentPlot(cx, cy, i)
        if adj and not pVis:IsVisible(adj:GetX(), adj:GetY()) then
            table.insert(fog, dirNames[i+1])
        end
    end
    if #fog > 0 then
        print("FOG|" .. cx .. "," .. cy .. "|" .. table.concat(fog, ","))
    end
end
{checks}
print("{SENTINEL}")
"""


def parse_fog_neighbor_response(
    lines: list[str],
) -> dict[tuple[int, int], list[str]]:
    """Parse FOG|x,y|dir1,dir2,... lines into {(x,y): [directions]}."""
    result: dict[tuple[int, int], list[str]] = {}
    for line in lines:
        if not line.startswith("FOG|"):
            continue
        parts = line.split("|")
        x_str, y_str = parts[1].split(",")
        result[(int(x_str), int(y_str))] = parts[2].split(",")
    return result


def diff_threats(
    before: list[ThreatInfo], after: list[ThreatInfo]
) -> tuple[list[ThreatInfo], list[ThreatInfo], list[ThreatInfo]]:
    """Compare threat snapshots: (disappeared, new, moved).

    Match by unit_id when available, otherwise by (owner_id, unit_type, x, y).
    """
    after_by_uid: dict[int, ThreatInfo] = {}
    after_by_key: dict[tuple, ThreatInfo] = {}
    after_matched: set[int] = set()

    for i, t in enumerate(after):
        if t.unit_id:
            after_by_uid[t.unit_id] = t
        after_by_key[(t.owner_id, t.unit_type, t.x, t.y)] = t

    disappeared: list[ThreatInfo] = []
    moved: list[ThreatInfo] = []

    for bt in before:
        at = None
        if bt.unit_id and bt.unit_id in after_by_uid:
            at = after_by_uid[bt.unit_id]
        elif (bt.owner_id, bt.unit_type, bt.x, bt.y) in after_by_key:
            at = after_by_key[(bt.owner_id, bt.unit_type, bt.x, bt.y)]

        if at is None:
            disappeared.append(bt)
        else:
            idx = after.index(at)
            after_matched.add(idx)
            if at.x != bt.x or at.y != bt.y:
                moved.append(at)

    new_threats = [t for i, t in enumerate(after) if i not in after_matched]
    return disappeared, new_threats, moved


def build_pathing_estimate_query(unit_index: int, target_x: int, target_y: int) -> str:
    """InGame context: estimate turns for a unit to reach a destination.

    Uses UnitManager.GetMoveToPath for the full path and
    UnitManager.GetReachableMovement for this-turn reachable tiles.
    """
    return f"""
{_lua_get_unit(unit_index)}
-- Guard: GetMoveToPath returns degenerate paths for units with 0 moves
if unit:GetMovesRemaining() <= 0 then
    print("PATH|-2|0|0")
    print("WAYPOINTS|")
    print("{SENTINEL}")
    return
end
local targetPlot = Map.GetPlot({target_x}, {target_y})
if not targetPlot then {_bail(f"ERR:INVALID_TARGET|Target ({target_x},{target_y}) is out of bounds")} end
local path = UnitManager.GetMoveToPath(unit, targetPlot:GetIndex())
if not path or #path == 0 then
    print("PATH|-1|0|0")
    print("WAYPOINTS|")
    print("{SENTINEL}")
    return
end
-- Validate path reaches destination (GetMoveToPath returns garbage for unreachable targets)
local lastPlot = Map.GetPlotByIndex(path[#path])
if lastPlot:GetX() ~= {target_x} or lastPlot:GetY() ~= {target_y} then
    print("PATH|-1|" .. #path .. "|0")
    print("WAYPOINTS|")
    print("{SENTINEL}")
    return
end
local reach = UnitManager.GetReachableMovement(unit)
local reachSet = {{}}
if reach then
    for _, pIdx in ipairs(reach) do reachSet[pIdx] = true end
end
-- Count how many path tiles are reachable this turn
local reachCount = 0
for _, pIdx in ipairs(path) do
    if reachSet[pIdx] then reachCount = reachCount + 1 end
end
local totalTiles = #path
local tilesPerTurn = math.max(reachCount, 1)
local turnsNeeded
if reachCount >= totalTiles then
    turnsNeeded = 0
else
    turnsNeeded = math.ceil((totalTiles - reachCount) / tilesPerTurn)
end
print("PATH|" .. turnsNeeded .. "|" .. totalTiles .. "|" .. reachCount)
-- Emit waypoints for context (first tile, last reachable, destination)
local waypoints = {{}}
for i, pIdx in ipairs(path) do
    local plot = Map.GetPlotByIndex(pIdx)
    waypoints[#waypoints + 1] = "(" .. plot:GetX() .. "," .. plot:GetY() .. ")"
end
print("WAYPOINTS|" .. table.concat(waypoints, ";"))
print("{SENTINEL}")
"""


def parse_pathing_estimate(lines: list[str]) -> PathingEstimate:
    """Parse PATH| and WAYPOINTS| output."""
    est = PathingEstimate(turns=0, total_tiles=0, reachable_this_turn=0, waypoints=[])
    for line in lines:
        if line.startswith("PATH|"):
            parts = line.split("|")
            if len(parts) >= 4:
                est.turns = int(parts[1])
                est.total_tiles = int(parts[2])
                est.reachable_this_turn = int(parts[3])
        elif line.startswith("WAYPOINTS|"):
            est.waypoints = line.split("|", 1)[1].split(";")
    return est


# ── Post-move visibility ────────────────────────────────────────────────


def build_post_move_visibility_query(now_x: int, now_y: int, radius: int = 4) -> str:
    """GameCore: scan tiles around a position and return revealed tile data.

    Used after a unit move to compute newly-revealed tiles via Python-side diff.
    Radius 4 covers all standard sight ranges (2 for most units, 3 for scouts).
    Output: ``TILE|x,y|terrain|feature|resource:class|hills|camp|units|city``
    """
    return f"""
local cx, cy, r = {now_x}, {now_y}, {radius}
local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local pTech = Players[me]:GetTechs()
{_LUA_RES_VISIBLE}
for dy = -r, r do
    for dx = -r, r do
        local x, y = cx + dx, cy + dy
        local plot = Map.GetPlot(x, y)
        if plot and vis:IsRevealed(plot:GetX(), plot:GetY()) then
            local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
            local feature = "none"
            local fi = plot:GetFeatureType()
            if fi >= 0 then feature = GameInfo.Features[fi].FeatureType end
            local resource = "none"
            local ri = plot:GetResourceType()
            if ri >= 0 then
                local re = GameInfo.Resources[ri]
                if resVisible(re) then
                    resource = re.ResourceType .. ":" .. (re.ResourceClassType or "")
                end
            end
            local hills = plot:IsHills() and "1" or "0"
            local camp = "0"
            local ii = plot:GetImprovementType()
            if ii >= 0 then
                local iInfo = GameInfo.Improvements[ii]
                if iInfo and iInfo.ImprovementType == "IMPROVEMENT_BARBARIAN_CAMP" then
                    camp = "1"
                end
            end
            local units = "none"
            if vis:IsVisible(plot:GetX(), plot:GetY()) then
                local uParts = {{}}
                for pid = 0, 63 do
                    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
                        for _, u in Players[pid]:GetUnits():Members() do
                            if u:GetX() == x and u:GetY() == y then
                                local entry = GameInfo.Units[u:GetType()]
                                local nm = entry and entry.UnitType or "UNKNOWN"
                                local ownerLabel = "Barbarian"
                                if pid ~= 63 then
                                    local cfg = PlayerConfigurations[pid]
                                    if cfg then ownerLabel = Locale.Lookup(cfg:GetCivilizationShortDescription()) end
                                end
                                table.insert(uParts, ownerLabel .. " " .. nm:gsub("UNIT_", ""))
                            end
                        end
                    end
                end
                if #uParts > 0 then units = table.concat(uParts, ";") end
            end
            local cityName = "none"
            if plot:IsCity() then
                local cOwner = plot:GetOwner()
                if cOwner >= 0 and cOwner ~= me then
                    pcall(function()
                        for _, c in Players[cOwner]:GetCities():Members() do
                            if c:GetX() == x and c:GetY() == y then
                                cityName = Locale.Lookup(c:GetName())
                                break
                            end
                        end
                    end)
                end
            end
            print("TILE|" .. x .. "," .. y .. "|" .. terrain .. "|" .. feature .. "|" .. resource .. "|" .. hills .. "|" .. camp .. "|" .. units .. "|" .. cityName)
        end
    end
end
print("{SENTINEL}")
"""


def parse_post_move_visibility(
    lines: list[str],
) -> list[tuple[int, int, dict]]:
    """Parse TILE| lines from post-move visibility query.

    Returns (x, y, metadata) tuples where metadata contains terrain, feature,
    resource, resource_class, hills, camp, units, and city fields.
    """
    results: list[tuple[int, int, dict]] = []
    for line in lines:
        if not line.startswith("TILE|"):
            continue
        parts = line.split("|")
        if len(parts) < 9:
            continue
        xy = parts[1].split(",")
        x, y = int(xy[0]), int(xy[1])
        # Parse resource into name + class
        resource = None
        resource_class = None
        if parts[4] != "none":
            rp = parts[4].split(":", 1)
            resource = rp[0]
            if len(rp) > 1 and rp[1]:
                resource_class = rp[1].replace("RESOURCECLASS_", "").lower()
        meta = {
            "terrain": parts[2],
            "feature": None if parts[3] == "none" else parts[3],
            "resource": resource,
            "resource_class": resource_class,
            "hills": parts[5] == "1",
            "camp": parts[6] == "1",
            "units": None if parts[7] == "none" else parts[7].split(";"),
            "city": None if parts[8] == "none" else parts[8],
        }
        results.append((x, y, meta))
    return results


def build_builder_tasks_query() -> str:
    """InGame context: scans all owned tiles for improvement tasks and all idle builders.

    Outputs TASK| lines for tiles needing work and BUILDER| lines for builder units.
    Uses hardcoded resource mapping and terrain heuristics for improvement recommendations.
    Does NOT use CanStartOperation with remote tiles (corrupts engine state → crash).
    """
    return """
local me = Game.GetLocalPlayer()
local pCities = Players[me]:GetCities()

-- 改良是否已解锁(前置科技/市政都满足)。
-- 2026-08-07 苏美尔局实测:任务板把「(58,30) 建伐木场」列为可做任务,
-- 但伐木场要「机械」科技,当时没研 —— 派建造者过去才发现做不了,白扔一回合。
-- 根因是"同一个概念两套实现":资源格分支【有】科技检查,空地分支【没有】。
-- 统一成这一个函数,两条分支都走它。
local function impUnlocked(impType)
    local row = impType and GameInfo.Improvements[impType]
    if not row then return true end          -- 查不到就放行(fail-open)
    local pt = row.PrereqTech
    if pt and pt ~= "" then
        local t = GameInfo.Technologies[pt]
        if t and not Players[me]:GetTechs():HasTech(t.Index) then return false end
    end
    local pc = row.PrereqCivic
    if pc and pc ~= "" then
        local c = GameInfo.Civics[pc]
        if c and not Players[me]:GetCulture():HasCivic(c.Index) then return false end
    end
    return true
end

-- Gather all builders with charges
local builders = {}
for _, u in Players[me]:GetUnits():Members() do
    local entry = GameInfo.Units[u:GetType()]
    if entry and entry.UnitType == "UNIT_BUILDER" and u:GetBuildCharges() > 0 then
        local bx, by = u:GetX(), u:GetY()
        if bx ~= -9999 then
            table.insert(builders, {id=u:GetID(), idx=u:GetID() % 65536, x=bx, y=by, charges=u:GetBuildCharges(), moves=u:GetMovesRemaining()})
        end
    end
end

-- Hardcoded resource -> improvement mapping (avoids GameInfo.Improvement_ValidResources()
-- iterator which can crash the game engine with EXCEPTION_ACCESS_VIOLATION)
local resImpMap = {
    -- Strategic
    RESOURCE_HORSES="IMPROVEMENT_PASTURE", RESOURCE_IRON="IMPROVEMENT_MINE",
    RESOURCE_NITER="IMPROVEMENT_MINE", RESOURCE_COAL="IMPROVEMENT_MINE",
    RESOURCE_ALUMINUM="IMPROVEMENT_MINE", RESOURCE_URANIUM="IMPROVEMENT_MINE",
    RESOURCE_OIL="IMPROVEMENT_OIL_WELL",
    -- Luxury (mined/quarried)
    RESOURCE_DIAMONDS="IMPROVEMENT_MINE", RESOURCE_JADE="IMPROVEMENT_MINE",
    RESOURCE_MERCURY="IMPROVEMENT_MINE", RESOURCE_SALT="IMPROVEMENT_MINE",
    RESOURCE_SILVER="IMPROVEMENT_MINE", RESOURCE_AMBER="IMPROVEMENT_MINE",
    RESOURCE_GYPSUM="IMPROVEMENT_QUARRY", RESOURCE_MARBLE="IMPROVEMENT_QUARRY",
    -- Luxury (plantation)
    RESOURCE_CITRUS="IMPROVEMENT_PLANTATION", RESOURCE_COCOA="IMPROVEMENT_PLANTATION",
    RESOURCE_COFFEE="IMPROVEMENT_PLANTATION", RESOURCE_COTTON="IMPROVEMENT_PLANTATION",
    RESOURCE_DYES="IMPROVEMENT_PLANTATION", RESOURCE_INCENSE="IMPROVEMENT_PLANTATION",
    RESOURCE_OLIVES="IMPROVEMENT_PLANTATION", RESOURCE_SILK="IMPROVEMENT_PLANTATION",
    RESOURCE_SPICES="IMPROVEMENT_PLANTATION", RESOURCE_SUGAR="IMPROVEMENT_PLANTATION",
    RESOURCE_TEA="IMPROVEMENT_PLANTATION", RESOURCE_TOBACCO="IMPROVEMENT_PLANTATION",
    RESOURCE_WINE="IMPROVEMENT_PLANTATION",
    -- Luxury (camp)
    RESOURCE_FURS="IMPROVEMENT_CAMP", RESOURCE_IVORY="IMPROVEMENT_CAMP",
    RESOURCE_TRUFFLES="IMPROVEMENT_CAMP", RESOURCE_HONEY="IMPROVEMENT_CAMP",
    -- Bonus
    RESOURCE_BANANAS="IMPROVEMENT_PLANTATION", RESOURCE_CATTLE="IMPROVEMENT_PASTURE",
    RESOURCE_SHEEP="IMPROVEMENT_PASTURE", RESOURCE_DEER="IMPROVEMENT_CAMP",
    RESOURCE_COPPER="IMPROVEMENT_MINE", RESOURCE_STONE="IMPROVEMENT_QUARRY",
    RESOURCE_MAIZE="IMPROVEMENT_FARM", RESOURCE_RICE="IMPROVEMENT_FARM",
    RESOURCE_WHEAT="IMPROVEMENT_FARM",
    -- Water (builders can't reach, but listed for completeness)
    RESOURCE_FISH="IMPROVEMENT_FISHING_BOATS", RESOURCE_CRABS="IMPROVEMENT_FISHING_BOATS",
    RESOURCE_PEARLS="IMPROVEMENT_FISHING_BOATS", RESOURCE_TURTLES="IMPROVEMENT_FISHING_BOATS",
    RESOURCE_WHALES="IMPROVEMENT_FISHING_BOATS",
}

-- Scan city territory for tasks
local seen = {}
local normalCount = 0
local maxNormal = 20
for _, city in pCities:Members() do
    local cx, cy = city:GetX(), city:GetY()
    local cityName = Locale.Lookup(city:GetName())
    for dy = -3, 3 do for dx = -3, 3 do
        local px, py = cx + dx, cy + dy
        local key = px .. "," .. py
        if not seen[key] then
            seen[key] = true
            local plot = Map.GetPlot(px, py)
            if plot and plot:GetOwner() == me and not plot:IsWater() and not plot:IsMountain() then
                local distIdx = plot:GetDistrictType()
                local impIdx = plot:GetImprovementType()
                local resIdx = plot:GetResourceType()

                -- Skip tiles with districts
                if distIdx < 0 then
                    -- Check for pillaged improvements
                    if impIdx >= 0 then
                        local okP, pil = pcall(function() return plot:IsImprovementPillaged() end)
                        if okP and pil then
                            local impInfo = GameInfo.Improvements[impIdx]
                            local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
                            -- Find nearest builder
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            print("TASK|urgent|" .. px .. "," .. py .. "|REPAIR|" .. impName:gsub("IMPROVEMENT_", "") .. "|pillaged|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                        end
                    -- Check for unimproved resource tiles
                    elseif resIdx >= 0 and impIdx < 0 then
                        local resInfo = GameInfo.Resources[resIdx]
                        if resInfo then
                            local resClass = resInfo.ResourceClassType or ""
                            local resName = resInfo.ResourceType:gsub("RESOURCE_", "")
                            local priority = "normal"
                            if resClass == "RESOURCECLASS_STRATEGIC" then priority = "urgent"
                            elseif resClass == "RESOURCECLASS_LUXURY" then priority = "high"
                            elseif resClass == "RESOURCECLASS_BONUS" then priority = "high"
                            end
                            -- Find valid improvement via resource lookup table
                            local validImp = resImpMap[resInfo.ResourceType] or "UNKNOWN"
                            -- Check tech/civic prerequisite(与空地分支同一个函数)
                            if validImp ~= "UNKNOWN" and not impUnlocked(validImp) then
                                validImp = validImp .. "_LOCKED"
                            end
                            -- 2026-08-10 罗马局 T193(缺陷76):上面只查【改良】的前置,
                            -- 而铀矿/煤矿的门槛在【资源】上(煤要工业化、铀要联合作战);
                            -- 矿井改良本身远古就解锁了,于是任务板把三块建不了的资源格
                            -- 列成 URGENT,三个建造者走过去全部 CANNOT_IMPROVE。
                            -- 资源自己的 PrereqTech 必须一起查,并把缺的科技名写进任务里,
                            -- 否则调用方只知道"做不了",不知道"还差什么、值不值得等"。
                            local resLock = nil
                            pcall(function()
                                local rpt = resInfo.PrereqTech
                                if rpt and rpt ~= "" then
                                    local rt = GameInfo.Technologies[rpt]
                                    if rt and not Players[me]:GetTechs():HasTech(rt.Index) then
                                        resLock = rpt:gsub("TECH_", "")
                                    end
                                end
                            end)
                            if resLock then
                                if not validImp:find("_LOCKED") then
                                    validImp = validImp .. "_LOCKED"
                                end
                                resName = resName .. "(需科技 " .. resLock .. ")"
                            end
                            -- Find nearest builder
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            local classShort = "bonus"
                            if resClass == "RESOURCECLASS_STRATEGIC" then classShort = "strategic"
                            elseif resClass == "RESOURCECLASS_LUXURY" then classShort = "luxury"
                            end
                            -- 2026-08-09 T282 实机:铀矿格被 FEATURE_FOREST 压着,任务板照喊
                            -- build MINE,improve 到场才报 CANNOT_IMPROVE。资源格与空地同规则:
                            -- 可移除地貌挡住除其专属改良外的一切改良 —— 任务描述必须预警。
                            local fIdxR = plot:GetFeatureType()
                            local fInfoR = fIdxR >= 0 and GameInfo.Features[fIdxR] or nil
                            if fInfoR then
                                local fNameR = fInfoR.FeatureType or ""
                                local removableR = false
                                local okRR = pcall(function() removableR = (fInfoR.Removable == true) end)
                                if not okRR or fInfoR.Removable == nil then
                                    removableR = (fNameR == "FEATURE_FOREST"
                                              or fNameR == "FEATURE_JUNGLE"
                                              or fNameR == "FEATURE_MARSH")
                                end
                                if removableR and validImp ~= "IMPROVEMENT_LUMBER_MILL" and validImp ~= "UNKNOWN" then
                                    resName = resName .. "(" .. fNameR:gsub("FEATURE_", "")
                                        .. " 挡着,先 remove_feature 再 improve)"
                                end
                            end
                            print("TASK|" .. priority .. "|" .. px .. "," .. py .. "|" .. validImp .. "|" .. resName .. "|" .. classShort .. "|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                        end
                    -- Check for empty tiles that could use standard improvements (capped)
                    -- Uses terrain heuristics instead of CanStartOperation (which corrupts
                    -- engine state when called with remote tile coordinates, causing
                    -- EXCEPTION_ACCESS_VIOLATION during end_turn)
                    elseif impIdx < 0 and resIdx < 0 and normalCount < maxNormal then
                        local featureIdx = plot:GetFeatureType()
                        local terrIdx = plot:GetTerrainType()
                        local terrInfo = terrIdx >= 0 and GameInfo.Terrains[terrIdx] or nil
                        local terrName = terrInfo and terrInfo.TerrainType or ""
                        local bestImp = nil
                        local needRemove = nil
                        local fInfo0 = featureIdx >= 0 and GameInfo.Features[featureIdx] or nil
                        local fName0 = fInfo0 and fInfo0.FeatureType or ""
                        -- ⚠️ 地貌一律先判,判完再看地形/丘陵(2026-08-07 两次实机)。
                        --
                        -- 第一次(丛林):`if plot:IsHills()` 排在最前面,于是【丛林丘陵】
                        --   被推荐"建矿"。当时的修法是把丛林/沼泽提到前面。
                        -- 第二次(森林):**森林没提上来**,还留在 IsHills() 后面 ——
                        --   (59,32) PLAINS_HILLS + FOREST 又被推荐"建矿",建造者白跑
                        --   两回合才发现做不了。修了一半的经典形态。
                        --
                        -- 所以这次不再逐个列举地貌名。规则改成:
                        --   【可移除的地貌】挡住除它专属改良之外的一切改良。
                        -- Removable 由运行时 GameInfo.Features 提供(导出表没抓这一列);
                        -- 读不到就退回三名单 —— Civ6 可移除地表只有森林/丛林/沼泽。
                        local removable = false
                        if fInfo0 then
                            local okR = pcall(function() removable = (fInfo0.Removable == true) end)
                            if not okR or fInfo0.Removable == nil then
                                removable = (fName0 == "FEATURE_FOREST"
                                          or fName0 == "FEATURE_JUNGLE"
                                          or fName0 == "FEATURE_MARSH")
                            end
                        end
                        local isWonder = false
                        pcall(function() isWonder = (fInfo0 and fInfo0.NaturalWonder == true) end)
                        if isWonder then
                            -- 自然奇观格不改良
                        elseif removable then
                            -- 森林专属:伐木场(需机械)。其余改良必须先砍掉地貌。
                            if fName0 == "FEATURE_FOREST" and impUnlocked("IMPROVEMENT_LUMBER_MILL") then
                                bestImp = "IMPROVEMENT_LUMBER_MILL"
                            else
                                -- 不静默跳过 —— 砍地貌本身是该做的活(给产能锤,
                                -- 并解锁下面的农场/矿)。列成明确的 REMOVE 任务,
                                -- 建造者才不会既不来也不知道该来干什么。
                                needRemove = fName0
                            end
                        elseif plot:IsHills() then
                            bestImp = "IMPROVEMENT_MINE"
                        elseif terrName == "TERRAIN_DESERT" or terrName == "TERRAIN_SNOW" or terrName == "TERRAIN_TUNDRA" then
                            -- Low-yield terrain, skip
                        else
                            bestImp = "IMPROVEMENT_FARM"
                        end
                        -- 前置科技/市政没到的改良不进任务板:派过去也做不了,白扔一回合。
                        -- ⚠️ 这里的 fail 方向与政策空槽提醒相反:那边数据缺失要 fail-open
                        -- (漏报=静默一条真建议),这边要 fail-closed —— 建造者是全局最稀缺
                        -- 的资源,推荐一个做不了的活等于直接扣掉两回合。
                        if bestImp and not impUnlocked(bestImp) then bestImp = nil end
                        if bestImp then
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            print("TASK|normal|" .. px .. "," .. py .. "|" .. bestImp .. "||none|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                            normalCount = normalCount + 1
                        elseif needRemove then
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            print("TASK|normal|" .. px .. "," .. py .. "|REMOVE_FEATURE|" .. needRemove:gsub("FEATURE_", "") .. "|remove|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                            normalCount = normalCount + 1
                        end
                    end
                end
            end
        end
    end end
end

-- Print builder info
for _, b in ipairs(builders) do
    print("BUILDER|" .. b.id .. "|" .. b.idx .. "|" .. b.x .. "," .. b.y .. "|" .. b.charges .. "|" .. string.format("%.1f", b.moves))
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def parse_builder_tasks(
    lines: list[str],
) -> tuple[list[BuilderTask], list[BuilderInfo]]:
    """Parse TASK| and BUILDER| lines from build_builder_tasks_query."""
    tasks: list[BuilderTask] = []
    builders: list[BuilderInfo] = []

    for line in lines:
        try:
            if line.startswith("TASK|"):
                parts = line.split("|")
                if len(parts) < 9:
                    continue
                xy = parts[2].split(",")
                if len(xy) != 2:
                    continue
                imp = parts[3]
                # Skip tasks where tech prerequisite isn't met
                if imp.endswith("_LOCKED"):
                    continue
                tasks.append(
                    BuilderTask(
                        priority=parts[1],
                        x=int(xy[0]),
                        y=int(xy[1]),
                        improvement=imp,
                        resource=parts[4],
                        resource_class=parts[5],
                        city_name=parts[6],
                        nearest_builder_id=int(parts[7]),
                        distance=int(parts[8]),
                    )
                )
            elif line.startswith("BUILDER|"):
                parts = line.split("|")
                if len(parts) < 6:
                    continue
                xy = parts[3].split(",")
                if len(xy) != 2:
                    continue
                builders.append(
                    BuilderInfo(
                        unit_id=int(parts[1]),
                        unit_index=int(parts[2]),
                        x=int(xy[0]),
                        y=int(xy[1]),
                        charges=int(parts[4]),
                        moves=float(parts[5]),
                    )
                )
        except (ValueError, IndexError):
            continue

    return tasks, builders
