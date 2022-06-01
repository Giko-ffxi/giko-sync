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
    local linkshell = {}
    local ok, code = http.request({method = "GET", url = config.tod.get, headers = {authorization = "Basic " .. (mime.b64(config.auth.user .. ':' .. config.auth.password))}, sink = function(chunk) if chunk ~= nil and string.match(chunk, '.+\|\{[^}]*\}') then resp = resp .. chunk end return true end })

    if ok and code == 200 and resp ~= '' then

        for r in string.gmatch(resp, "[^\r\n]+") do
            
            local mob, data = string.match(r, "(.+\)|(\{[^}]*\})")
            
            local s_tod = json:decode(data)
            local c_tod = death.get_tod(mob)

            if c_tod == nil or (c_tod.created_at ~= nil and c_tod.created_at < s_tod.created_at) then                

                if s_tod.gmt ~= c_tod.gmt or s_tod.day ~= c_tod.day then
                    table.insert(linkshell, string.format("[ToD][%s][%s][%s][Sheet update]", monster.get(mob).names.nq[1], common.gmt_to_local_date(s_tod.gmt), s_tod.day))
                end

                c_tods[mob] = json:encode(s_tod)
                u_flag = true

            end

            s_tods[mob] = s_tod

        end

        if u_flag then

            cache.set(death.cache, c_tods)         

            for k,v in ipairs(linkshell) do      
                ashita.timer.create(string.format('giko-sync-%s', k), (k * 2), 1, function() chat.linkshell(v) end)                
            end    

            chat.command('giko alerts reload')

        end

    end

    return s_tods

end

tod.set = function(s_tods)

    local resp   = ''    
    local lines  = cache.get_all(death.cache)
    local writer = io.open(death.cache, 'r')
    local c_tods = {}

    for mob,tod in pairs(lines) do

        local c_tod = json:decode(tod)
        local s_tod = s_tods[mob]

        if s_tod ~= nil and c_tod.created_at > s_tod.created_at and (c_tod.gmt ~= s_tod.gmt or c_tod.day ~= s_tod.day) then
            c_tods[mob] = json:encode(c_tod)
        end

    end
    
    if common.size(c_tods) > 0 then
        http.request({method = "POST", url = config.tod.set, headers = {authorization = "Basic " .. (mime.b64(config.auth.user .. ':' .. config.auth.password)), ["content-type"] = "application/x-www-form-urlencoded", ["content-length"] = string.len(string.format('tod=%s', json:encode(c_tods)))}, source = ltn12.source.string(string.format('tod=%s', json:encode(c_tods))), sink = function(chunk) if chunk ~= nil then resp = resp .. chunk end return true end })
    end

    return c_tods

end

return tod
