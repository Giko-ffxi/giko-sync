local timer   = require('lib.ashita.timer')
local cache   = require('lib.giko.cache')
local config  = require('lib.giko.config')
local common  = require('lib.giko.common')
local monster = require('lib.giko.monster')
local death   = require('lib.giko.death')
local chat    = require('lib.giko.chat')
local json    = require('json.json')
local http    = require('socket.http')
local ltn12   = require("ltn12")

local tod = {}

tod.get = function()

    local resp = ''
    local c_tods = {}
    local s_tods = {}
    local u_flag = false
    local tell = {}
    local ok, code = http.request({method = "GET", url = config.tod.get, headers = {authorization = "Basic " .. (mime.b64(config.auth.user .. ':' .. config.auth.password))}, sink = function(chunk) if chunk ~= nil and string.match(chunk, '.+\|\{[^}]*\}') then resp = resp .. chunk end return true end })

    if ok and code == 200 and resp ~= '' then

        for r in string.gmatch(resp, "[^\r\n]+") do
            
            local name, data = string.match(r, "(.+\)|(\{[^}]*\})")
            
            local mob   = monster.get(name)
            local s_tod = json:decode(data)
            local c_tod = (mob ~= nil and cache.get(death.cache, mob.names.nq[1]) and json:decode(cache.get(death.cache, mob.names.nq[1]))) or nil

            if c_tod == nil or (c_tod.created_at ~= nil and c_tod.created_at < s_tod.created_at) then                

                if c_tod == nil or s_tod.gmt ~= c_tod.gmt or s_tod.day ~= c_tod.day then
                    table.insert(tell, string.format("@giko set-tod %s %s %s --force", mob.names.nq[1], common.gmt_to_local_date(s_tod.gmt), s_tod.day or 0))
                end

                c_tods[string.lower(mob.names.nq[1])] = json:encode(s_tod)
                u_flag = true

            end

            s_tods[string.lower(mob.names.nq[1])] = s_tod

        end

        if u_flag then

            for k,v in ipairs(tell) do      
                ashita.timer.create(string.format('giko-sync-%s', k), (k * 2), 1, function() chat.tell(config.broadcaster, v) end)                
            end    

        end

    end

    return s_tods

end

tod.set = function(s_tods)

    local resp   = ''    
    local lines  = cache.get_all(death.cache)
    local writer = io.open(death.cache, 'r')
    local c_tods = {}

    for name, tod in pairs(lines) do

        local mob = monster.get(name)

        if common.in_array_key(s_tods, string.lower(mob.names.nq[1])) then

            local c_tod = json:decode(tod)
            local s_tod = s_tods[string.lower(mob.names.nq[1])]

            if s_tod ~= nil and c_tod.created_at > s_tod.created_at and (c_tod.gmt ~= s_tod.gmt or c_tod.day ~= s_tod.day) then
                c_tods[string.lower(mob.names.nq[1])] = json:encode(c_tod)
            end

        end

    end
    
    if common.size(c_tods) > 0 then
        
        http.request({method = "POST", url = config.tod.set, headers = {authorization = "Basic " .. (mime.b64(config.auth.user .. ':' .. config.auth.password)), ["content-type"] = "application/x-www-form-urlencoded", ["content-length"] = string.len(string.format('tod=%s', json:encode(c_tods)))}, source = ltn12.source.string(string.format('tod=%s', json:encode(c_tods))), sink = function(chunk) if chunk ~= nil then resp = resp .. chunk end return true end })
    
    end

    return c_tods

end

return tod
