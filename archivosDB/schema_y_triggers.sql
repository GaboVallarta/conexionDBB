-- ============================================================================
-- Respaldo de Automatización SQL - Sistema IoT de Transporte Público
-- Motor: PostgreSQL (Supabase)
-- ============================================================================

-- 1. Cerrar brecha de escalada de privilegios y limpiar duplicados en PERFILES
drop policy if exists "Usuarios actualizan su propio perfil" on public.perfiles;
drop policy if exists "Usuarios ven su propio perfil" on public.perfiles;

-- 2. Permitir que el trigger actualice UNIDADES sin chocar con RLS
create or replace function public.actualizar_estado_camion()
returns trigger
language plpgsql
security definer set search_path = ''
as $$
begin
  update public.unidades
  set 
    ultima_parada_id = new.parada_id,
    pasajeros_actuales = new.pasajeros_totales
  where id = new.unidad_id;
  
  return new;
end;
$$;

-- 3. Permitir al admin gestionar UNIDADES (crear/editar/borrar camiones)
drop policy if exists "Admins gestionan unidades" on public.unidades;
create policy "Admins gestionan unidades"
  on public.unidades for all
  using ( public.es_admin() )
  with check ( public.es_admin() );

-- 4. Permitir al hardware actualizar el estado vivo en REGISTROS_PARADA (upsert/update)
drop policy if exists "Hardware actualiza estado de parada" on public.registros_parada;
create policy "Hardware actualiza estado de parada"
  on public.registros_parada for update
  to authenticated
  using ( true )
  with check ( true );

-- 5. Limpiar política SELECT duplicada en RUTAS
drop policy if exists "Lectura publica de paradas" on public.rutas;
