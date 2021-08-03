from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError
from odoo.tools import float_is_zero, float_repr
from odoo.addons.stock_account.models.product import ProductTemplate as OriginalProductTemplate
from odoo.addons.stock_account.models.product import ProductProduct as OriginalProductProduct

def write(self, vals):
    impacted_templates = {}
    move_vals_list = []
    Product = self.env['product.product']
    SVL = self.env['stock.valuation.layer']

    if 'categ_id' in vals:
        # When a change of category implies a change of cost method, we empty out and replenish
        # the stock.
        new_product_category = self.env['product.category'].browse(vals.get('categ_id'))

        for product_template in self:
            valuation_impacted = False
            if product_template.cost_method != new_product_category.property_cost_method:
                valuation_impacted = True
            if product_template.valuation != new_product_category.property_valuation:
                valuation_impacted = True
            if valuation_impacted is False:
                continue

            # Empty out the stock with the current cost method.
            description = _("Due to a change of product category (from %s to %s), the costing method\
                            has changed for product template %s: from %s to %s.") %\
                (product_template.categ_id.display_name, new_product_category.display_name, \
                 product_template.display_name, product_template.cost_method, new_product_category.property_cost_method)
            out_svl_vals_list, products_orig_quantity_svl, products = Product\
                ._svl_empty_stock(description, product_template=product_template)
            out_stock_valuation_layers = SVL.create(out_svl_vals_list)
            if product_template.valuation == 'real_time':
                move_vals_list += Product._svl_empty_stock_am(out_stock_valuation_layers)
            impacted_templates[product_template] = (products, description, products_orig_quantity_svl)

    res = super(ProductTemplate, self).write(vals)

    for product_template, (products, description, products_orig_quantity_svl) in impacted_templates.items():
        # Replenish the stock with the new cost method.
        in_svl_vals_list = products._svl_replenish_stock(description, products_orig_quantity_svl)
        in_stock_valuation_layers = SVL.create(in_svl_vals_list)
        if product_template.valuation == 'real_time':
            move_vals_list += Product._svl_replenish_stock_am(in_stock_valuation_layers)

    # Create the account moves.
    if move_vals_list:
        account_moves = self.env['account.move'].create(move_vals_list)
        # removed 08/03/2021
        account_moves.post()
    return res

OriginalProductTemplate.write = write

def _change_standard_price(self, new_price, counterpart_account_id=False):
    """Helper to create the stock valuation layers and the account moves
    after an update of standard price.

    :param new_price: new standard price
    """
    # Handle stock valuation layers.
    svl_vals_list = []
    company_id = self.env.company
    for product in self:
        if product.cost_method not in ('standard', 'average'):
            continue
        quantity_svl = product.sudo().quantity_svl
        if float_is_zero(quantity_svl, precision_rounding=product.uom_id.rounding):
            continue
        diff = new_price - product.standard_price
        value = company_id.currency_id.round(quantity_svl * diff)
        if company_id.currency_id.is_zero(value):
            continue

        svl_vals = {
            'company_id': company_id.id,
            'product_id': product.id,
            'description': _('Product value manually modified (from %s to %s)') % (product.standard_price, new_price),
            'value': value,
            'quantity': 0,
        }
        svl_vals_list.append(svl_vals)
    stock_valuation_layers = self.env['stock.valuation.layer'].sudo().create(svl_vals_list)

    # Handle account moves.
    product_accounts = {product.id: product.product_tmpl_id.get_product_accounts() for product in self}
    am_vals_list = []
    for stock_valuation_layer in stock_valuation_layers:
        product = stock_valuation_layer.product_id
        value = stock_valuation_layer.value

        if product.type != 'product' or product.valuation != 'real_time':
            continue

        # Sanity check.
        if counterpart_account_id is False:
            raise UserError(_('You must set a counterpart account.'))
        if not product_accounts[product.id].get('stock_valuation'):
            raise UserError(_('You don\'t have any stock valuation account defined on your product category. You must define one before processing this operation.'))

        if value < 0:
            debit_account_id = counterpart_account_id
            credit_account_id = product_accounts[product.id]['stock_valuation'].id
        else:
            debit_account_id = product_accounts[product.id]['stock_valuation'].id
            credit_account_id = counterpart_account_id

        move_vals = {
            'journal_id': product_accounts[product.id]['stock_journal'].id,
            'company_id': company_id.id,
            'ref': product.default_code,
            'stock_valuation_layer_ids': [(6, None, [stock_valuation_layer.id])],
            'line_ids': [(0, 0, {
                'name': _('%s changed cost from %s to %s - %s') % (self.env.user.name, product.standard_price, new_price, product.display_name),
                'account_id': debit_account_id,
                'debit': abs(value),
                'credit': 0,
                'product_id': product.id,
            }), (0, 0, {
                'name': _('%s changed cost from %s to %s - %s') % (self.env.user.name, product.standard_price, new_price, product.display_name),
                'account_id': credit_account_id,
                'debit': 0,
                'credit': abs(value),
                'product_id': product.id,
            })],
        }
        am_vals_list.append(move_vals)
    account_moves = self.env['account.move'].create(am_vals_list)
    # removed 08/03/2021
    # if account_moves:
    #     account_moves.post()

    # Actually update the standard price.
    self.with_context(force_company=company_id.id).sudo().write({'standard_price': new_price})

OriginalProductProduct._change_standard_price = _change_standard_price
