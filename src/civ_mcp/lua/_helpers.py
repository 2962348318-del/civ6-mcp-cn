"""Internal helpers — reduce boilerplate in Lua query/action builders."""

from __future__ import annotations

SENTINEL = "---END---"

# Item type → GameInfo table name (shared by produce + purchase builders)
_ITEM_TABLE_MAP: dict[str, str] = {
    "UNIT": "Units",
    "BUILDING": "Buildings",
    "DISTRICT": "Districts",
    "PROJECT": "Projects",
}

# Item type → CityOperationTypes param key (shared by produce + purchase builders)
_ITEM_PARAM_MAP: dict[str, str] = {
    "UNIT": "PARAM_UNIT_TYPE",
    "BUILDING": "PARAM_BUILDING_TYPE",
    "DISTRICT": "PARAM_DISTRICT_TYPE",
    "PROJECT": "PARAM_PROJECT_TYPE",
}

# Item type → GameInfo row 的类型名列(ITEM_NOT_FOUND 报近似名用)
_ITEM_TYPECOL_MAP: dict[str, str] = {
    "UNIT": "UnitType",
    "BUILDING": "BuildingType",
    "DISTRICT": "DistrictType",
    "PROJECT": "ProjectType",
}


def _bail(msg: str) -> str:
    """Python-side helper that expands to the Lua bail pattern.

    Usage in f-strings: ``if cond then {_bail("ERR:REASON")} end``
    Generates: ``print("ERR:REASON"); print("---END---"); return``
    """
    return f'print("{msg}"); print("{SENTINEL}"); return'


def _bail_lua(lua_expr: str) -> str:
    """Like _bail but the argument is a raw Lua expression (for string concatenation).

    Usage in f-strings: ``if cond then {_bail_lua('"ERR:REASON|" .. luaVar')} end``
    Generates: ``print("ERR:REASON|" .. luaVar); print("---END---"); return``
    """
    return f'print({lua_expr}); print("{SENTINEL}"); return'


def _lua_close_diplo_session() -> str:
    """Lua snippet: close any open diplomacy session with ``target``, restore UI.

    Expects ``me`` and ``target`` to be defined in scope.
    """
    return (
        "for r = 1, 5 do "
        "sid = DiplomacyManager.FindOpenSessionID(me, target) "
        "if not sid or sid < 0 then break end "
        'DiplomacyManager.AddResponse(sid, me, "NEGATIVE") '
        "sid = DiplomacyManager.FindOpenSessionID(me, target) "
        "if not sid or sid < 0 then break end "
        "DiplomacyManager.CloseSession(sid) "
        "end "
        "LuaEvents.DiplomacyActionView_ShowIngameUI() "
        "pcall(function() Events.HideLeaderScreen() end)"
    )


# Unit IDs are (generation << 16) | slot.  FindID() only matches the low 16
# bits, and slots are recycled when a unit dies.  Measured 2026-07-31:
#   unit A = 0x100001 (gen 16, slot 1) — killed
#   unit B = 0x110001 (gen 17, slot 1) — created, reused the slot
#   FindID(0x100001) then silently returned B
# Acting on a stale ID therefore hits a *different* unit with no error at all.
# The generation check below turns that silent mis-targeting into an explicit
# failure.  Values below 65536 are treated as legacy slot numbers and skip the
# check, so older call sites keep working unchanged.
_STALE_MSG = (
    "ERR:UNIT_STALE|That unit ID belongs to a dead unit; its slot has been "
    "reused by a different unit. Call get_units to refresh unit IDs."
)


def _lua_unit_lookup(unit_id: int) -> str:
    """Lua snippet: FindID + generation check. Shared by both contexts."""
    return (
        f"local me = Game.GetLocalPlayer() "
        f"local unit = Players[me]:GetUnits():FindID({unit_id}) "
        f"if unit == nil then {_bail('ERR:UNIT_NOT_FOUND')} end "
        f"if {unit_id} > 65535 and unit:GetID() ~= {unit_id} then "
        f"{_bail(_STALE_MSG)} end"
    )


def _lua_get_unit(unit_id: int) -> str:
    """Lua snippet: look up a unit in InGame context or bail."""
    return _lua_unit_lookup(unit_id)


def _lua_get_unit_gamecore(unit_id: int) -> str:
    """Lua snippet: look up a unit in GameCore context or bail."""
    return _lua_unit_lookup(unit_id)


def _lua_get_city(city_id: int) -> str:
    """Lua snippet: look up a city in InGame context or bail."""
    return (
        f"local me = Game.GetLocalPlayer() "
        f"local pCity = CityManager.GetCity(me, {city_id} % 65536) "
        f"if pCity == nil then {_bail('ERR:CITY_NOT_FOUND')} end"
    )


def _lua_op_target_plots() -> str:
    """Lua snippet: split a ``targets`` table from CityManager.GetOperationTargets.

    Defines ``plotIndices`` (buildable tiles) and ``failReason`` (the game's own
    explanation when there are none).

    2026-08-08 T190 实测缺陷:区域顾问和奇观顾问各自写了一份
    ``for k,v in pairs(targets) do if type(v)=="table" then ...`` —— 不看 key,
    把返回表里【每个】table 值都当成地块索引。GetOperationTargets 的返回是按
    CityOperationResults.* 分键的:PLOTS 才是可建地块,还并列着 BLOCKED_PLOTS、
    MOUNTAIN_PLOTS 和 FAILURE_REASONS。后果两头都错:

      · 巴地比拉建工业区,返回里只有 FAILURE_REASONS = {"人口需要达到7或更多。"},
        中文字符串被当成地块索引,Map.GetPlotByIndex 退化成 0 号地块(0,0 水格),
        被水格过滤掉 → 报"没有可放置的地块"。游戏明明给了确切原因,被扔了。
      · 反向同样成立:BLOCKED_PLOTS / MOUNTAIN_PLOTS 会被当作合法地块推荐出去。

    所以这里收敛成一个函数,两个顾问共用 —— 这类"一个概念两套实现"在本项目
    已经反复出问题,修一套忘一套。
    """
    return """
local plotIndices = {}
local failReason = ""
local RES = CityOperationResults
for k, v in pairs(targets) do
    if type(v) == "table" then
        if RES ~= nil and k == RES.PLOTS then
            for _, idx in ipairs(v) do table.insert(plotIndices, idx) end
        elseif RES ~= nil and k == RES.FAILURE_REASONS then
            for _, r in ipairs(v) do
                if failReason ~= "" then failReason = failReason .. " / " end
                failReason = failReason .. tostring(r)
            end
        elseif RES == nil then
            -- 常量表缺席时的退化路径:只收数字条目,绝不让字符串冒充地块索引
            for _, idx in ipairs(v) do
                if type(idx) == "number" then table.insert(plotIndices, idx) end
            end
        end
    end
end
failReason = failReason:gsub("%[ICON_[^%]]*%]", ""):gsub("^%s+", ""):gsub("%s+$", "")
"""


# ---------------------------------------------------------------------------
# Parser helpers — reduce noise in pipe-delimited response parsers
# ---------------------------------------------------------------------------


def _int(s: str) -> int:
    """Parse a string that may be a float representation to int.

    Lua prints integers as floats (e.g. ``3.0``).  This avoids the
    ``int(float(x))`` pattern repeated across every parser.
    """
    return int(float(s))


# ---------------------------------------------------------------------------
# Shared Lua snippet constants — compose into builders via string concat.
# These are plain strings (NOT f-strings), so Lua braces are unescaped.
# Interpolate into f-string builders with {_LUA_RES_VISIBLE} etc.
# ---------------------------------------------------------------------------

# Resource visibility check — expects ``pTech`` in scope.
_LUA_RES_VISIBLE = """\
local function resVisible(resEntry)
    if not resEntry.PrereqTech then return true end
    local t = GameInfo.Technologies[resEntry.PrereqTech]
    return t and pTech:HasTech(t.Index)
end"""

# Hex 距离(odd-r offset → cube)。与 Python 侧 civ_mcp/hexgrid.py 同公式。
# 2026-08-04 P0:Map.GetPlotDistance 在 offset 网格上有实测误报
# (units.py 攻击门禁注释、方案 v5 铁律五),一切距离判定用本函数,
# 不用 max(|dx|,|dy|) 也不用 GetPlotDistance。
_LUA_HEX_DIST = """\
local function hexDist(x1, y1, x2, y2)
    local q1 = x1 - math.floor((y1 - (y1 % 2)) / 2)
    local r1 = y1
    local q2 = x2 - math.floor((y2 - (y2 % 2)) / 2)
    local r2 = y2
    local s1 = -q1 - r1
    local s2 = -q2 - r2
    return math.floor((math.abs(q1 - q2) + math.abs(r1 - r2) + math.abs(s1 - s2)) / 2)
end"""

# 近战跨域铁律。近战攻击在 Civ6 里就是「带 ATTACK 修饰的移动到目标格」,
# 所以陆战单位打不到水面上的目标,海战单位打不到陆地上的目标
# (沿海城市除外 —— 海军近战可以打城)。
#
# 危险之处在于引擎不拒绝:CanStartOperation(MOVE_TO + ATTACK) 对这两种
# 跨域组合返回 true,RequestOperation 却静默什么都不做 —— 表现为
# 「报了 OK:MELEE_ATTACK,双方 HP 纹丝不动,移动力也没扣」,白扔一回合。
# 2026-08-07 实机(苏美尔国王局 T114/T115):战车连着两回合打 (65,33) 的
# 海上蛮族桨帆船,两次都报成功,桨帆船始终 45/100。
#
# 这与 2026-08-04 修掉的「远程单位降级近战打海上 GALLEY 报假成功」是
# 同一个病灶的另一半:那次修的是【远程执行路径】,这次是【近战执行路径】
# 加上【CAN ATTACK 枚举】。三处必须共用这一个判定 —— 只修执行路径的话
# get_units 会继续把打不了的目标列出来,只修枚举的话 force 攻击照样白扔。
_LUA_MELEE_DOMAIN = """\
local function meleeDomainOK(atkEntry, tx, ty)
    local dom = atkEntry and atkEntry.Domain or nil
    if dom ~= "DOMAIN_LAND" and dom ~= "DOMAIN_SEA" then return true, "" end
    local p = Map.GetPlot(tx, ty)
    if p == nil then return true, "" end
    -- 两个探测都 fail-open:读不到就不拦。误拦一次合法攻击的代价
    -- (养成无脑 force 的习惯)高于漏拦一次跨域攻击。
    local water = false
    pcall(function() water = p:IsWater() end)
    local hasCity, cityKnown = false, false
    pcall(function() hasCity = Cities.GetCityInPlot(tx, ty) ~= nil; cityKnown = true end)
    if dom == "DOMAIN_LAND" and water then
        return false, "LAND_VS_WATER"
    end
    if dom == "DOMAIN_SEA" and (not water) and cityKnown and (not hasCity) then
        return false, "SEA_VS_LAND"
    end
    return true, ""
end"""

# Victory-enabled check — prints VENABLED| lines for each enabled victory type.
# Yield label table + formatters for trade route yield display.
# fmtY: format array of {YieldIndex, Amount} objects (from GetOutgoingRoutes).
# fmtFlat/sumFlat: format/sum flat 6-element arrays (from Calculate* APIs).
# Both share the yN label table.
_LUA_YIELD_LABELS = 'local yN = {"F","P","G","S","C","A"}'

_LUA_FMT_Y = """\
local function fmtY(tbl)
    if not tbl then return "" end
    local s = ""
    for _, e in ipairs(tbl) do
        if e.Amount and e.Amount > 0 then
            local idx = e.YieldIndex + 1
            if idx >= 1 and idx <= 6 then
                local amt = e.Amount
                if amt == math.floor(amt) then amt = math.floor(amt) end
                s = s .. yN[idx] .. amt
            end
        end
    end
    return s
end"""

_LUA_FMT_FLAT = """\
local function sumFlat(...)
    local s = {0,0,0,0,0,0}
    for _, t in ipairs({...}) do
        if t then for j = 1, 6 do s[j] = s[j] + (t[j] or 0) end end
    end
    return s
end
local function fmtFlat(arr)
    if not arr then return "" end
    local s = ""
    for j = 1, 6 do
        local v = arr[j]
        if v and v > 0 then
            if v == math.floor(v) then v = math.floor(v) end
            s = s .. yN[j] .. v
        end
    end
    return s
end"""

# XP threshold calculation for unit promotions.
# Expects ``exp`` (unit:GetExperience()), ``promClass`` (PromotionClass string) in scope.
# Produces: xpPromoCount, xp, xpNeeded.
# Uses GetExperienceForNextLevel() directly — the game engine already
# accounts for current level when returning the threshold.
_LUA_XP_THRESHOLD = """\
local xpPromoCount = 0
if promClass ~= "" then
    for p in GameInfo.UnitPromotions() do
        if p.PromotionClass == promClass and exp:HasPromotion(p.Index) then
            xpPromoCount = xpPromoCount + 1
        end
    end
end
local xp = exp:GetExperiencePoints()
local xpNeeded = exp:GetExperienceForNextLevel()"""

# UU 替换解析 —— 把基础单位映射为本文明的独特替换单位(如 SWORDSMAN →
# ROMAN_LEGION)。升级链/升级执行/单位列表共用;引擎侧数据
# (GameInfo.UnitReplaces + CivilizationTraits)是权威,不依赖导出表。
# 2026-08-02 校准局 0c:三处各自硬编码基础单位,导致罗马勇士的升级目标
# 显示为剑士(需铁,库存 0)而实际引擎升出的是军团(不需铁)——
# agent 据显示会主动放弃这次本可行的升级。
_LUA_RESOLVE_UU = """\
local function resolveUU(baseType)
    if baseType == nil or baseType == "" then return baseType end
    local ok, resolved = pcall(function()
        local myCiv = PlayerConfigurations[Game.GetLocalPlayer()]:GetCivilizationTypeName()
        local traits = {}
        for row in GameInfo.CivilizationTraits() do
            if row.CivilizationType == myCiv then traits[row.TraitType] = true end
        end
        for row in GameInfo.UnitReplaces() do
            if row.ReplacesUnitType == baseType then
                local uu = GameInfo.Units[row.CivUniqueUnitType]
                if uu and uu.TraitType and traits[uu.TraitType] then
                    return row.CivUniqueUnitType
                end
            end
        end
        return baseType
    end)
    if ok and resolved then return resolved end
    return baseType
end"""

_LUA_VICTORY_ENABLED = """\
local _vtypes = {"VICTORY_TECHNOLOGY","VICTORY_CULTURE","VICTORY_RELIGIOUS","VICTORY_DIPLOMATIC","VICTORY_CONQUEST"}
for _, vt in ipairs(_vtypes) do
    local row = GameInfo.Victories[vt]
    if row then
        local ok, en = pcall(function() return Game.IsVictoryEnabled(row.Index) end)
        if ok and en then print("VENABLED|" .. vt) end
    end
end"""
