import discord
from sqlalchemy import func, select

from vpi_bot.models import (
    ConstructionProject,
    Country,
    CountryLore,
    CountryResource,
    CountryTechnology,
    EconomicOperation,
    EconomicStats,
    GamePost,
    HistoricalEvent,
    IndustryFacility,
    MilitaryStats,
    ResearchProject,
    ResourceDefinition,
    ResourceOperation,
    Verdict,
)
from vpi_bot.ui.cards import country_overview_embed


class CountrySelect(discord.ui.Select):
    def __init__(self, bot, owner_id: int, countries: list[Country]):
        options = [
            discord.SelectOption(label=row.name[:100], value=str(row.id)) for row in countries[:25]
        ]
        super().__init__(
            placeholder="Выберите государство", options=options, min_values=1, max_values=1
        )
        self.bot, self.owner_id = bot, owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Этот выбор принадлежит другому игроку.", ephemeral=True
            )
            return
        country_id = int(self.values[0])
        async with self.bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id,
                    Country.owner_id == self.owner_id,
                    Country.guild_id == interaction.guild_id,
                    Country.is_active.is_(True),
                )
            )
            stats = await session.get(EconomicStats, country_id) if country else None
            army = await session.get(MilitaryStats, country_id) if country else None
            resource_count = (
                await session.scalar(
                    select(func.count(CountryResource.resource_id)).where(
                        CountryResource.country_id == country_id
                    )
                )
                if country
                else 0
            )
        if not country or not stats:
            await interaction.response.send_message(
                "Данные государства не найдены.", ephemeral=True
            )
            return
        embed = country_overview_embed(country, stats, army, resource_count=resource_count or 0)
        await interaction.response.edit_message(
            embed=embed, view=CountryPanel(self.bot, country.id, self.owner_id)
        )


class CountrySelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, countries: list[Country]):
        super().__init__(timeout=180)
        self.add_item(CountrySelect(bot, owner_id, countries))


class CountryPanel(discord.ui.View):
    def __init__(self, bot, country_id: int, owner_id: int):
        super().__init__(timeout=300)
        self.bot, self.country_id, self.owner_id = bot, country_id, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта панель принадлежит другому игроку.", ephemeral=True
            )
            return False
        async with self.bot.db() as session:
            country = await session.get(Country, self.country_id)
        if (
            not country
            or country.guild_id != interaction.guild_id
            or not country.is_active
            or country.owner_id != interaction.user.id
        ):
            await interaction.response.send_message(
                "Владелец государства изменился; откройте панель заново.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Экономика", emoji="💰", style=discord.ButtonStyle.primary)
    async def economy(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            country = await session.get(Country, self.country_id)
            stats = await session.get(EconomicStats, self.country_id)
            if not country or not stats:
                await interaction.response.send_message(
                    "Данные государства не найдены.", ephemeral=True
                )
                return
            embed = discord.Embed(
                title=f"💰 Экономика государства — {country.name}",
                description="Основные показатели и состояние экономики",
                color=0x2ECC71
                if stats.stability >= 70
                else 0xF1C40F
                if stats.stability >= 40
                else 0xE74C3C,
            )
            for label, key, suffix in [
                ("💰 Казна", "treasury", country.currency),
                ("📈 ВВП", "gdp", country.currency),
                ("⬆️ Доходы", "income", country.currency),
                ("⬇️ Расходы", "expenses", country.currency),
                ("🔥 Инфляция", "inflation", "%"),
                ("👥 Безработица", "unemployment", "%"),
                ("🌱 Рост ВВП", "growth", "%"),
                ("🏦 Долг", "debt", country.currency),
                ("🏭 Промышленность", "industry_level", "ур."),
            ]:
                value = getattr(stats, key)
                text = (
                    f"{int(value):,} {suffix}"
                    if key == "industry_level"
                    else f"{value:,.2f} {suffix}"
                )
                embed.add_field(name=label, value=text, inline=True)
            stability = float(stats.stability)
            filled = max(0, min(10, round(stability / 10)))
            embed.add_field(
                name="🛡️ Стабильность",
                value=f"{'🟩' * filled}{'⬜' * (10 - filled)} {stability:.1f}%",
                inline=False,
            )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Ресурсы", emoji="⛏️", style=discord.ButtonStyle.secondary)
    async def resources(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            rows = (
                await session.execute(
                    select(
                        ResourceDefinition.name, CountryResource.quantity, ResourceDefinition.unit
                    )
                    .join(CountryResource, CountryResource.resource_id == ResourceDefinition.id)
                    .where(CountryResource.country_id == self.country_id)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        embed = discord.Embed(title=f"📦 Склад — {country.name}", color=0x2ECC71)
        embed.description = (
            "\n".join(f"**{name}:** {qty:,.3f} {unit}" for name, qty, unit in rows)[:3900]
            or "Склад пока пуст."
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Общая информация", emoji="🏛️", style=discord.ButtonStyle.secondary)
    async def overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            country = await session.get(Country, self.country_id)
            stats = await session.get(EconomicStats, self.country_id)
            army = await session.get(MilitaryStats, self.country_id)
            resource_count = await session.scalar(
                select(func.count(CountryResource.resource_id)).where(
                    CountryResource.country_id == self.country_id
                )
            )
        embed = country_overview_embed(country, stats, army, resource_count=resource_count or 0)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="История", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            verdicts = (
                await session.scalars(
                    select(Verdict)
                    .join(GamePost, Verdict.post_id == GamePost.id)
                    .where(GamePost.country_id == self.country_id)
                    .order_by(Verdict.created_at.desc())
                    .limit(5)
                )
            ).all()
            events = (
                await session.scalars(
                    select(HistoricalEvent)
                    .where(
                        HistoricalEvent.country_id == self.country_id,
                        HistoricalEvent.verdict_id.is_(None),
                    )
                    .order_by(HistoricalEvent.event_date.desc())
                    .limit(3)
                )
            ).all()
            resource_ops = (
                await session.execute(
                    select(
                        ResourceDefinition.name,
                        ResourceOperation.delta,
                        ResourceOperation.source,
                        ResourceOperation.created_at,
                    )
                    .join(ResourceOperation, ResourceOperation.resource_id == ResourceDefinition.id)
                    .where(ResourceOperation.country_id == self.country_id)
                    .order_by(ResourceOperation.created_at.desc())
                    .limit(3)
                )
            ).all()
            money_ops = (
                await session.scalars(
                    select(EconomicOperation)
                    .where(EconomicOperation.country_id == self.country_id)
                    .order_by(EconomicOperation.created_at.desc())
                    .limit(3)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        lines = [
            f"⚖️ **{row.title}** — {row.status.replace('_', ' ')} · {row.state}" for row in verdicts
        ]
        lines += [f"📌 **{row.title}** — {row.content[:180]}" for row in events]
        lines += [
            f"⛏ {name} {delta:+g} · {source} · {created:%d.%m.%Y}"
            for name, delta, source, created in resource_ops
        ]
        lines += [
            f"💰 {row.category} {row.delta:+g} · {row.source} · {row.created_at:%d.%m.%Y}"
            for row in money_ops
        ]
        embed = discord.Embed(
            title=f"📜 История — {country.name}",
            description="\n".join(lines[:15]) or "История пока пуста.",
            color=0x8E44AD,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Промышленность", emoji="🏭", style=discord.ButtonStyle.secondary, row=1
    )
    async def industry(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            rows = (
                await session.scalars(
                    select(IndustryFacility)
                    .where(IndustryFacility.country_id == self.country_id)
                    .order_by(IndustryFacility.name)
                    .limit(25)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        lines = [
            f"**{row.name}** · {row.facility_type} · ур. {row.level} · {row.status}" for row in rows
        ]
        embed = discord.Embed(
            title=f"🏭 Промышленность — {country.name}",
            description="\n".join(lines) or "Предприятия не зарегистрированы.",
            color=0xD35400,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Армия", emoji="🛡️", style=discord.ButtonStyle.secondary, row=1)
    async def military(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            army = await session.get(MilitaryStats, self.country_id)
            country = await session.get(Country, self.country_id)
        embed = discord.Embed(title=f"🛡️ Вооружённые силы — {country.name}", color=0x34495E)
        if army:
            embed.add_field(name="Личный состав", value=f"{army.personnel:,}", inline=True)
            embed.add_field(
                name="Уровень военных технологий", value=str(army.technology_level), inline=True
            )
            embed.add_field(
                name="Военный бюджет", value=f"{army.budget:,.2f} {country.currency}", inline=True
            )
            for label, value in (
                ("🟢 Готовность", float(army.readiness)),
                ("📦 Снабжение", float(army.supply)),
            ):
                filled = max(0, min(10, round(value / 10)))
                embed.add_field(
                    name=label,
                    value=f"{'🟩' * filled}{'⬜' * (10 - filled)} {value:.0f}%",
                    inline=True,
                )
            for label, values in (
                ("🪖 Подразделения", army.units),
                ("⚙️ Техника", army.equipment),
                ("🎯 Боеприпасы", army.ammunition),
            ):
                lines = [f"• {key}: {amount:g}" for key, amount in values.items()]
                embed.add_field(
                    name=label,
                    value="\n".join(lines[:8])[:900] or "Нет данных",
                    inline=False,
                )
        else:
            embed.description = "Военная статистика ещё не внесена администратором."
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Технологии", emoji="🔬", style=discord.ButtonStyle.secondary, row=2)
    async def technologies(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            rows = (
                await session.scalars(
                    select(CountryTechnology)
                    .where(CountryTechnology.country_id == self.country_id)
                    .order_by(CountryTechnology.level.desc())
                    .limit(25)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        lines = [f"**{row.name}** · ур. {row.level} · {row.status}" for row in rows]
        embed = discord.Embed(
            title=f"🔬 Технологии — {country.name}",
            description="\n".join(lines) or "Исследования не зарегистрированы.",
            color=0x1ABC9C,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Лор", emoji="📖", style=discord.ButtonStyle.secondary, row=2)
    async def lore(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            rows = (
                await session.scalars(
                    select(CountryLore)
                    .where(CountryLore.country_id == self.country_id)
                    .order_by(CountryLore.importance.desc(), CountryLore.updated_at.desc())
                    .limit(5)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        embed = discord.Embed(title=f"📖 Лор — {country.name}", color=0x9B59B6)
        for row in rows:
            embed.add_field(
                name=f"{row.category} · {row.title}"[:256], value=row.content[:1024], inline=False
            )
        if not rows:
            embed.description = "Записи лора отсутствуют."
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Как играть", emoji="❓", style=discord.ButtonStyle.secondary, row=3)
    async def help_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🎮 Краткая памятка игрока",
            description=(
                "1. В приватном канале государства напишите пост и ответьте на него `!verdict`.\n"
                "2. Либо отправьте одной командой `!verdict Текст поста`.\n"
                "3. Пост не должен превышать **2000 символов**; обычные сообщения не обрабатываются.\n"
                "4. Бот ответит карточкой с итогом, изменениями и остатком постов за сегодня."
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="Команды панели",
            value="`/panel` — открыть карточки страны · `/register` — зарегистрировать страну",
            inline=False,
        )
        embed.set_footer(text="Если вердикт ожидает проверки, изменения ещё не применены")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Проекты", emoji="🏗️", style=discord.ButtonStyle.secondary, row=2)
    async def projects(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            rows = (
                await session.scalars(
                    select(ConstructionProject)
                    .where(ConstructionProject.country_id == self.country_id)
                    .order_by(ConstructionProject.created_at.desc())
                    .limit(20)
                )
            ).all()
            research_rows = (
                await session.scalars(
                    select(ResearchProject)
                    .where(ResearchProject.country_id == self.country_id)
                    .order_by(ResearchProject.created_at.desc())
                    .limit(20)
                )
            ).all()
            country = await session.get(Country, self.country_id)
        lines = [
            f"🔬 **{row.technology_name}** ур. {row.target_level} · {row.status} · "
            f"{min(row.completed_turns, row.duration_turns)}/{row.duration_turns} ходов"
            for row in research_rows
        ]
        lines += [
            f"**{row.name}** · {row.project_type} · {row.status} · "
            f"{min(row.completed_turns, row.duration_turns)}/{row.duration_turns} ходов"
            for row in rows
        ]
        embed = discord.Embed(
            title=f"🏗️ Проекты и исследования — {country.name}",
            description="\n".join(lines) or "Проектов пока нет.",
            color=0xE67E22,
        )
        await interaction.response.edit_message(embed=embed, view=self)
