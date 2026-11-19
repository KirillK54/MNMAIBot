import discord
from sqlalchemy import select

from vpi_bot.models import Epoch, GuildConfig
from vpi_bot.seed import seed_guild_defaults


def _year(year: int | None, *, future: bool = False) -> str:
    if year is None:
        return "далее" if future else "?"
    return f"{abs(year)} до н. э." if year < 0 else f"{year} н. э."


def _epoch_embed(epoch: Epoch, *, current_name: str) -> discord.Embed:
    start = _year(epoch.start_year)
    end = _year(epoch.end_year, future=True)
    embed = discord.Embed(
        title=f"🕰️ {epoch.name}",
        description=f"**Период:** {start} — {end}\n{epoch.description or 'Описание не задано.'}"[
            :4000
        ],
        color=0x5865F2,
    )
    embed.add_field(
        name="🔬 Доступные технологии",
        value=", ".join(epoch.technologies[:20])[:1000] or "Не заданы",
        inline=False,
    )
    if epoch.rules:
        embed.add_field(name="📜 Правила эпохи", value=epoch.rules[:1000], inline=False)
    if epoch.restrictions:
        forbidden = epoch.restrictions.get("forbidden_technologies", [])
        if forbidden:
            embed.add_field(
                name="⛔ Запрещено", value=", ".join(forbidden[:20])[:1000], inline=False
            )
    template = epoch.starting_stats or {}
    economy = template.get("economy", {})
    military = template.get("military", {})
    resources = template.get("resources", {})
    if template:
        lines = []
        for key, label in (
            ("treasury", "Казна"),
            ("gdp", "ВВП"),
            ("industry_level", "Промышленность"),
        ):
            if key in economy:
                lines.append(f"{label}: {economy[key]:,}")
        if "personnel" in military:
            lines.append(f"Армия: {military['personnel']:,} человек")
        if resources:
            lines.append("Стартовые запасы: " + ", ".join(list(resources)[:8]))
        embed.add_field(
            name="📊 Стартовые показатели новых стран",
            value="\n".join(lines)[:1000] or "Шаблон задан.",
            inline=False,
        )
    else:
        embed.add_field(
            name="📊 Стартовые показатели новых стран",
            value="Отдельного шаблона нет; будут использованы общие значения.",
            inline=False,
        )
    if epoch.name == current_name:
        embed.set_footer(text="Эта эпоха уже выбрана")
    else:
        embed.set_footer(text="Выбор не сбросит показатели уже зарегистрированных государств")
    return embed


def _epoch_options(epochs: list[Epoch], current_name: str) -> list[discord.SelectOption]:
    options = []
    current_marked = False
    for epoch in epochs[:25]:
        description = f"{_year(epoch.start_year)} — {_year(epoch.end_year, future=True)}"
        is_current = epoch.name == current_name and not current_marked
        current_marked = current_marked or is_current
        options.append(
            discord.SelectOption(
                label=epoch.name[:100],
                value=str(epoch.id),
                description=description[:100],
                default=is_current,
            )
        )
    return options


class _EpochSelect(discord.ui.Select):
    def __init__(self, epochs: list[Epoch], current_name: str):
        super().__init__(
            placeholder="Выберите эпоху по названию и периоду",
            options=_epoch_options(epochs, current_name),
            min_values=1,
            max_values=1,
            row=0,
        )
        self.epochs = {epoch.id: epoch for epoch in epochs[:25]}

    async def callback(self, interaction: discord.Interaction) -> None:
        view: EpochPickerView = self.view
        epoch = self.epochs.get(int(self.values[0]))
        if epoch is None:
            await interaction.response.send_message(
                "Эпоха не найдена; откройте меню заново.", ephemeral=True
            )
            return
        view.selected_epoch_id = epoch.id
        view.apply_button.disabled = False
        await interaction.response.edit_message(
            content="Проверьте описание и стартовые показатели, затем подтвердите выбор.",
            embed=_epoch_embed(epoch, current_name=view.current_name),
            view=view,
        )


class EpochPickerView(discord.ui.View):
    def __init__(
        self,
        bot,
        guild_id: int,
        owner_id: int,
        epochs: list[Epoch],
        current_name: str,
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.epochs = {epoch.id: epoch for epoch in epochs[:25]}
        self.current_name = current_name
        self.selected_epoch_id: int | None = None
        self.add_item(_EpochSelect(epochs, current_name))
        self.apply_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Это меню выбора открыто другим администратором.", ephemeral=True
            )
            return False
        if interaction.permissions.administrator:
            return True
        config = await self.bot.guild_config(self.guild_id)
        role_id = config.admin_role_id if config else None
        if role_id and any(role.id == role_id for role in getattr(interaction.user, "roles", ())):
            return True
        await interaction.response.send_message(
            "Недостаточно прав для смены эпохи.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Применить эпоху", emoji="✅", style=discord.ButtonStyle.success, row=1
    )
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_epoch_id is None:
            await interaction.response.send_message("Сначала выберите эпоху.", ephemeral=True)
            return
        from vpi_bot.epoch_service import apply_epoch_selection, refresh_country_epoch_cards

        try:
            name, has_template = await apply_epoch_selection(
                self.bot, self.guild_id, interaction.user.id, self.selected_epoch_id
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        note = (
            "Новые страны получат её стартовый шаблон."
            if has_template
            else "Для неё используются общие стартовые значения."
        )
        await interaction.response.edit_message(
            content=f"Текущая эпоха: **{name}**. {note} Показатели существующих стран сохранены.",
            embed=None,
            view=None,
        )
        self.stop()
        updated, failed = await refresh_country_epoch_cards(self.bot, self.guild_id)
        if failed:
            await interaction.followup.send(
                f"Карточки обновлены: {updated}; не удалось обновить: {failed}. Проверьте права бота.",
                ephemeral=True,
            )

    @discord.ui.button(label="Отмена", emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Выбор эпохи отменён.", embed=None, view=None
        )
        self.stop()


async def open_epoch_picker(bot, interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Меню доступно только на сервере.", ephemeral=True)
        return
    async with bot.db() as session:
        config = await session.get(GuildConfig, interaction.guild_id)
        if config is None or not await bot.guild_config(interaction.guild_id):
            await interaction.response.send_message(
                "Сначала настройте сервер через /setup.", ephemeral=True
            )
            return
        await seed_guild_defaults(session, interaction.guild_id)
        await session.commit()
        epochs = (
            await session.scalars(
                select(Epoch)
                .where(Epoch.guild_id == interaction.guild_id)
                .order_by(Epoch.start_year, Epoch.name)
            )
        ).all()
        current_name = config.epoch_name
    epochs = sorted(
        epochs, key=lambda epoch: (epoch.start_year is None, epoch.start_year or 0, epoch.name)
    )
    view = EpochPickerView(bot, interaction.guild_id, interaction.user.id, epochs, current_name)
    embed = discord.Embed(
        title="🕰️ Выбор игровой эпохи",
        description=(
            "Выберите эпоху в меню. Перед применением откроются её правила, технологии и стартовые "
            "показатели. Смена эпохи не сбрасывает статистику уже созданных стран."
        ),
        color=0x5865F2,
    )
    embed.set_footer(text=f"Сейчас выбрана: {current_name}")
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
